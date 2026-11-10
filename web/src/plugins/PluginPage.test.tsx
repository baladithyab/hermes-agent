// @vitest-environment jsdom
import { act, useEffect, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ProfileContext, type ProfileContextValue } from '@/contexts/profile-context';
import { setManagementProfile } from '@/lib/api';
import { PluginPage } from './PluginPage';
import { exposePluginSDK } from './registry';

vi.mock('@/i18n', () => ({ useI18n: () => ({ t: { common: { loading: 'Loading' } } }) }));

const containers: HTMLDivElement[] = [];
const roots: Root[] = [];

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', {
    status: 200, headers: { 'content-type': 'application/json' },
  })));
  exposePluginSDK();
});

afterEach(() => {
  for (const root of roots.splice(0)) act(() => root.unmount());
  for (const container of containers.splice(0)) container.remove();
  setManagementProfile('');
  vi.unstubAllGlobals();
});

describe('profile-bound plugin lifecycle', () => {
  it('remounts on selection changes, but pending calls keep their captured profile', async () => {
    const sdk = window.__HERMES_PLUGIN_SDK__!;
    const retired: string[] = [];
    const requests: Array<() => Promise<unknown>> = [];
    function Plugin() {
      const [profile] = useState(() => sdk.getManagementProfile());
      useEffect(() => {
        requests.push(() => sdk.fetchJSON(`/api/plugins/example/console/result?profile=${encodeURIComponent(profile)}`));
        return () => { retired.push(profile); };
      }, [profile]);
      return <span>{profile}</span>;
    }
    window.__HERMES_PLUGINS__!.register('profile-capture-test', Plugin);
    const container = document.createElement('div');
    document.body.append(container);
    containers.push(container);
    const root = createRoot(container);
    roots.push(root);
    const renderProfile = (profile: string, currentProfile = 'served', profiles = ['first', 'second']) => {
      setManagementProfile(profile);
      const context: ProfileContextValue = { profile, currentProfile, profiles, setProfile: () => {} };
      act(() => root.render(
        <ProfileContext.Provider value={context}><PluginPage name="profile-capture-test" /></ProfileContext.Provider>,
      ));
    };

    renderProfile('first');
    expect(container.textContent).toBe('first');
    renderProfile('second');
    expect(container.textContent).toBe('second');
    expect(retired).toEqual(['first']);
    expect(requests).toHaveLength(2);
    await requests[0]();
    await requests[1]();
    const urls = vi.mocked(fetch).mock.calls.map(([url]) => String(url));
    expect(urls).toEqual([
      '/api/plugins/example/console/result?profile=first',
      '/api/plugins/example/console/result?profile=second',
    ]);
    // Registry/profile inventory updates do not reset an unchanged selection.
    renderProfile('second', 'served', ['first', 'second', 'third']);
    expect(requests).toHaveLength(2);
    renderProfile('first');
    expect(requests).toHaveLength(3);
    expect(retired).toEqual(['first', 'second']);
    renderProfile('first', 'other-serving-home');
    expect(requests).toHaveLength(4);
  });

  it('uses the selected default for ordinary requests while preserving explicit profiles', async () => {
    const sdk = window.__HERMES_PLUGIN_SDK__!;
    setManagementProfile('other-home');
    await sdk.fetchJSON('/api/plugins/ordinary/items');
    setManagementProfile('later-home');
    await sdk.authedFetch('/api/plugins/ordinary/download?profile=original-home');
    await sdk.authedFetch('/api/plugins/ordinary/download');
    expect(vi.mocked(fetch).mock.calls.map(([url]) => String(url))).toEqual([
      '/api/plugins/ordinary/items?profile=other-home',
      '/api/plugins/ordinary/download?profile=original-home',
      '/api/plugins/ordinary/download?profile=later-home',
    ]);
  });
});
