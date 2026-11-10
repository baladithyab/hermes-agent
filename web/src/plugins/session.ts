import { fetchJSON } from '@/lib/api';
import type { FetchJSON, PluginSession, PluginResourceRequest } from './sdk';

interface Selection { scope_id: string; epoch: number }
interface Selected extends Selection { profile: string }
interface ClientState {
  client: string;
  epoch: number;
  active: symbol | null;
  selected: Selected | null;
  tail: Promise<void>;
}

// A window's replacement session revokes the previous acknowledged selection,
// rather than creating a second independent client whose old grants stay live.
const clients = new Map<string, ClientState>();

export function createPluginSession(pluginName: string, profile: string): PluginSession {
  if (!/^[A-Za-z0-9_-]+$/.test(pluginName) || typeof profile !== 'string' || !profile || profile.length > 128) {
    throw new Error('Invalid captured plugin profile');
  }
  let client = clients.get(pluginName);
  if (!client) {
    client = { client: crypto.randomUUID(), epoch: 0, active: null, selected: null, tail: Promise.resolve() };
    clients.set(pluginName, client);
  }
  const state = client;
  const id = Symbol(pluginName);
  state.active = id;
  let closed = false;
  let selected: Selected | null = null;
  const handles = new Set<() => Promise<void>>();

  const url = (path: string, capturedProfile = profile): string => {
    if (!path.startsWith('/') || path.startsWith('//') || path.includes('\\') || path.includes('#')) {
      throw new Error('Invalid plugin path');
    }
    const parsed = new URL(path, 'https://plugin.invalid');
    const decoded = decodeURIComponent(path.split('?')[0]);
    if (decoded.split('/').some(part => part === '.' || part === '..') || decoded.includes('\\')) {
      throw new Error('Invalid plugin path');
    }
    parsed.searchParams.set('profile', capturedProfile);
    return `/api/plugins/${pluginName}${parsed.pathname}${parsed.search}`;
  };
  const headers = (selection?: Selection, init?: RequestInit): Headers => {
    const result = new Headers(init?.headers);
    result.set('X-Hermes-Plugin-Client', state.client);
    result.set('X-Hermes-Plugin-Epoch', String(selection?.epoch ?? state.epoch));
    if (selection) result.set('X-Hermes-Plugin-Scope', selection.scope_id);
    else result.delete('X-Hermes-Plugin-Scope');
    return result;
  };
  const exchange = async (action: 'selection' | 'deselection', previous: Selected | null): Promise<Selection> => {
    const ack = await fetchJSON<Selection>(url(`/_host/${action}`, previous?.profile ?? profile), {
      method: 'POST', headers: headers(previous ?? undefined),
    });
    if (!ack || !/^[a-f0-9]{32}$/.test(ack.scope_id) || !Number.isSafeInteger(ack.epoch) || ack.epoch <= state.epoch) {
      throw new Error('STALE_SCOPE');
    }
    state.epoch = ack.epoch;
    return ack;
  };
  const ready = state.tail.then(async () => {
    if (state.selected) {
      await exchange('deselection', state.selected);
      state.selected = null;
    }
    if (closed || state.active !== id) return;
    const acknowledgment = await exchange('selection', null);
    selected = { ...acknowledgment, profile };
    state.selected = selected;
  });
  // An ambiguous selection write poisons the chain: never mint a replacement
  // identity/retry automatically after a lost acknowledgment.
  state.tail = ready;
  void ready.catch(() => undefined);

  const current = (): boolean => !closed && state.active === id && selected !== null && state.selected === selected;
  const sessionFetch: FetchJSON = async <T = unknown>(path: string, init?: RequestInit, options?: { allowUnauthorized?: boolean }): Promise<T> => {
    await ready;
    if (!current() || !selected) throw new Error('STALE_SCOPE');
    const result = await fetchJSON<T>(url(path), { ...init, headers: headers(selected, init) }, options);
    if (!current()) throw new Error('STALE_SCOPE');
    return result;
  };

  return {
    fetchJSON: sessionFetch,
    presentResource: async (request: PluginResourceRequest) => {
      await ready;
      if (!current() || !selected) throw new Error('STALE_SCOPE');
      const result = await fetchJSON<{ url: string; handle_id: string; expires_at: number }>(url('/_host/resource/open'), {
        method: 'POST', headers: headers(selected), body: JSON.stringify({ document_id: request.documentId, grant_id: request.grantId,
          entry_path: request.entryPath, expires_at: request.expiresAt }),
      });
      if (!result || typeof result.url !== 'string' || !/^p-[a-f0-9]{48}$/.test(result.handle_id)) {
        throw new Error('RESOURCE_RESPONSE_INVALID');
      }
      try {
        const presented = new URL(result.url);
        if (!['http:', 'https:'].includes(presented.protocol) || presented.username || presented.password ||
            presented.hostname.split('.')[0] !== result.handle_id || presented.pathname !== '/__hermes_plugin_resource__' ||
            presented.search || presented.hash) throw new Error();
      } catch { throw new Error('RESOURCE_RESPONSE_INVALID'); }
      const captured = selected;
      let pending: Promise<void> | undefined;
      const close = (): Promise<void> => {
        if (!pending) {
          handles.delete(close);
          pending = fetchJSON(url('/_host/resource/close'), { method: 'POST', headers: headers(captured),
            body: JSON.stringify({ handle_id: result.handle_id }) }).then(() => undefined);
        }
        return pending;
      };
      if (!current()) {
        await close().catch(() => undefined);
        throw new Error('STALE_SCOPE');
      }
      handles.add(close);
      return { url: result.url, close };
    },
    close: async () => {
      if (closed) return;
      closed = true;
      const cleanup = Promise.allSettled([...handles].map(close => close()));
      if (state.active === id) {
        state.active = null;
        const retirement = state.tail.then(async () => {
          await cleanup;
          if (state.selected === selected && selected) {
            await exchange('deselection', selected);
            state.selected = null;
          }
        });
        state.tail = retirement;
        void retirement.catch(() => undefined);
        await cleanup;
        await retirement;
      } else await cleanup;
    },
  };
}
