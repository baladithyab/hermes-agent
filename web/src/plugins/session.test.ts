import { beforeEach, describe, expect, it, vi } from 'vitest';

const http = vi.hoisted(() => vi.fn<(url: string, init?: RequestInit) => Promise<unknown>>());
vi.mock('@/lib/api', () => ({ fetchJSON: http }));
import { createPluginSession } from './session';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
}

let epoch = 0;
const resource = { documentId: 'cdce3827-ef75-4770-b081-491a58125eb2', grantId: 'G'.repeat(43),
  entryPath: '/v0/resource/plugins/demo/index.html', expiresAt: 1300 };
const handle = 'p-' + 'a'.repeat(48);
const opened = { url: `http://${handle}.localhost:8642/__hermes_plugin_resource__`, handle_id: handle, expires_at: 1300 };
const reply = (url: string): unknown => {
  const path = new URL(url, 'http://dashboard.test').pathname;
  if (path.endsWith('/_host/selection') || path.endsWith('/_host/deselection')) {
    epoch += 1;
    return { scope_id: epoch.toString(16).padStart(32, '0'), epoch };
  }
  if (path.endsWith('/_host/resource/open')) return opened;
  return { api_version: 1, data: { ok: true } };
};
beforeEach(() => { epoch = 0; http.mockReset(); http.mockImplementation(async url => reply(url)); });

describe('Dashboard captured plugin sessions', () => {
  it('owns explicit profile and acknowledged selection even when callers supply forged query/headers', async () => {
    const session = createPluginSession('captured-transport', 'alpha');
    const response = await session.fetchJSON('/console/prepare?profile=beta', {
      method: 'POST', headers: { 'X-Hermes-Plugin-Scope': 'forged', 'X-Hermes-Plugin-Epoch': '999' }, body: '{}',
    });
    expect(response).toEqual({ api_version: 1, data: { ok: true } });
    const [url, init] = http.mock.calls[1];
    expect(new URL(url, 'http://dashboard.test').searchParams.get('profile')).toBe('alpha');
    const headers = new Headers(init?.headers);
    expect(headers.get('X-Hermes-Plugin-Scope')).toBe('1'.padStart(32, '0'));
    expect(headers.get('X-Hermes-Plugin-Epoch')).toBe('1');
    expect(headers.get('X-Hermes-Plugin-Client')).toBeTruthy();
    await session.close();
    await expect(session.fetchJSON('/console/status')).rejects.toThrow('STALE_SCOPE');
  });

  it('revokes the old selection on remount and rejects old replies without retargeting their original request', async () => {
    const gate = deferred<unknown>();
    const started = deferred<void>();
    http.mockImplementation(async url => {
      if (url.includes('/pending?')) { started.resolve(); return gate.promise; }
      return reply(url);
    });
    const old = createPluginSession('replacement-session', 'alpha');
    await old.fetchJSON('/ready');
    const pending = old.fetchJSON('/pending');
    const rejected = expect(pending).rejects.toThrow('STALE_SCOPE');
    await started.promise;
    const replacement = createPluginSession('replacement-session', 'beta');
    await replacement.fetchJSON('/ready');
    gate.resolve({ secret: 'old-owner' });
    await rejected;
    const pendingCall = http.mock.calls.find(([url]) => url.includes('/pending?'))!;
    expect(new URL(pendingCall[0], 'http://dashboard.test').searchParams.get('profile')).toBe('alpha');
    const selections = http.mock.calls.filter(([url]) => url.includes('/_host/selection?'));
    expect(new Headers(selections[0][1]?.headers).get('X-Hermes-Plugin-Client')).toBe(new Headers(selections[1][1]?.headers).get('X-Hermes-Plugin-Client'));
    expect(http.mock.calls.some(([url]) => url.endsWith('/_host/deselection?profile=alpha'))).toBe(true);
    await old.close();
    await replacement.close();
  });

  it('does not retry or mint a new identity after an ambiguous selection write', async () => {
    http.mockRejectedValue(new Error('selection reply lost'));
    const first = createPluginSession('ambiguous-session', 'alpha');
    await expect(first.fetchJSON('/ready')).rejects.toThrow('selection reply lost');
    const replacement = createPluginSession('ambiguous-session', 'beta');
    await expect(replacement.fetchJSON('/ready')).rejects.toThrow('selection reply lost');
    expect(http).toHaveBeenCalledTimes(1);
  });

  it('presents with the same document/profile/selection and closes each local handle before deselection', async () => {
    const session = createPluginSession('resource-session', 'alpha');
    await session.fetchJSON('/console/unlock');
    const presented = await session.presentResource(resource);
    expect(presented.url).toBe(opened.url);
    const opening = http.mock.calls.find(([url]) => url.includes('/_host/resource/open?'))!;
    expect(JSON.parse(String(opening[1]?.body))).toEqual({ document_id: resource.documentId, grant_id: resource.grantId,
      entry_path: resource.entryPath, expires_at: resource.expiresAt });
    expect(new Headers(opening[1]?.headers).get('X-Hermes-Plugin-Scope')).toBe('1'.padStart(32, '0'));
    await presented.close();
    await presented.close();
    await session.close();
    const closing = http.mock.calls.filter(([url]) => url.includes('/_host/resource/close?'));
    expect(closing).toHaveLength(1);
    expect(JSON.parse(String(closing[0][1]?.body))).toEqual({ handle_id: handle });
    expect(http.mock.calls.at(-1)?.[0]).toContain('/_host/deselection?profile=alpha');
  });

  it('retires a late presentation on its old profile instead of returning it to a replacement mount', async () => {
    const gate = deferred<unknown>();
    const started = deferred<void>();
    http.mockImplementation(async url => {
      if (url.includes('/_host/resource/open?')) { started.resolve(); return gate.promise; }
      return reply(url);
    });
    const old = createPluginSession('late-presentation', 'alpha');
    const pending = old.presentResource(resource);
    const rejected = expect(pending).rejects.toThrow('STALE_SCOPE');
    await started.promise;
    const replacement = createPluginSession('late-presentation', 'beta');
    await replacement.fetchJSON('/ready');
    gate.resolve(opened);
    await rejected;
    const closing = http.mock.calls.find(([url]) => url.includes('/_host/resource/close?'))!;
    expect(closing[0]).toContain('profile=alpha');
    expect(new Headers(closing[1]?.headers).get('X-Hermes-Plugin-Scope')).toBe('1'.padStart(32, '0'));
    await replacement.close();
  });
});
