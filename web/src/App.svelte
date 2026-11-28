<script lang="ts">
  import { onMount, setContext } from 'svelte';
  import { Client, type Administrator, type SignedIn } from './lib/api';
  import { CALLBACK, place, pathOf, TABS, SETUP_NAVIGATION, type Callback, type SetupLanding, type SetupNavigation } from './lib/routes';
  import { apply, store, stored, type Theme } from './lib/theme';
  import SignIn from './screens/SignIn.svelte';
  import Enrol from './screens/Enrol.svelte';
  import Shell from './screens/Shell.svelte';

  interface SessionInfo extends SignedIn {
    expires_at: string;
  }

  const client = new Client();
  let view = $state<'loading' | 'signin' | 'enrol' | 'shell'>('loading');
  let admin = $state<Administrator | null>(null);
  let token = $state('');
  let tab = $state(TABS[0]?.id ?? '');
  let landing = $state.raw<SetupLanding | null>(null);
  setContext<SetupNavigation>(SETUP_NAVIGATION, (href) => {
    const url = new URL(href, location.href);
    if (url.origin !== location.origin) return false;
    const where = place(url.pathname, TABS, url.search);
    if (where.kind !== 'tab' || !where.landing) return false;
    tab = where.tab;
    landing = where.landing;
    history.pushState(null, '', url.pathname + url.search);
    return true;
  });
  let theme = $state<Theme>('dark');
  // BR-216: a provider's return, held in memory only until it is posted under the session
  let callback = $state.raw<Callback | null>(null);

  function local(): Storage | undefined {
    try {
      return window.localStorage;
    } catch {
      return undefined;
    }
  }

  function setTheme(next: Theme): void {
    theme = next;
    apply(next);
    store(local(), next);
  }

  function enter(who: Administrator): void {
    admin = who;
    view = 'shell';
    if (!landing) history.replaceState(null, '', callback ? CALLBACK : pathOf(tab));
  }

  function callbackDone(): void {
    callback = null;
    goto('tree');
  }

  function signedOut(): void {
    admin = null;
    client.csrf = '';
    view = 'signin';
  }

  function goto(id: string): void {
    landing = null;
    tab = id;
    history.pushState(null, '', pathOf(id));
  }

  onMount(() => {
    setTheme(stored(local()));
    // SCR-03.session-expired: any 401 returns to sign-in, then to the same screen — also in the
    // session the enrolment page opened, which never went through the branch below.
    client.onUnauthenticated = signedOut;
    const onPop = (): void => {
      const now = place(location.pathname);
      if (now.kind === 'tab') { tab = now.tab; landing = null; }
    };
    window.addEventListener('popstate', onPop);
    const stop = (): void => window.removeEventListener('popstate', onPop);
    const where = place(location.pathname, TABS, location.search);
    if (where.kind === 'callback') {
      // BR-216: `code` and `state` leave the address bar before any request
      history.replaceState(null, '', CALLBACK);
      callback = where.callback;
    }
    if (where.kind === 'enrol') {
      // BR-203: the link leaves the address bar before any request after the shell's own.
      token = where.token;
      history.replaceState(null, '', '/admin/enrol');
      view = 'enrol';
      return stop;
    }
    if (where.kind === 'tab') { tab = where.tab; landing = where.landing ?? null; }
    client
      .call<SessionInfo>('GET', 'session')
      .then((s) => enter(client.signedIn(s)))
      .catch(() => {
        view = 'signin';
      });
    return stop;
  });
</script>

{#if view === 'signin'}
  <SignIn {client} onSignedIn={enter} />
{:else if view === 'enrol'}
  <Enrol {client} {token} onSignedIn={enter} />
{:else if view === 'shell' && admin}
  <Shell
    {client}
    {admin}
    {tab}
    {landing}
    {theme}
    {callback}
    onCallbackDone={callbackDone}
    onTab={goto}
    onTheme={setTheme}
    onSignedOut={signedOut}
  />
{/if}
