<script lang="ts">
  // SCR-03: top bar (wordmark, tabs from the route table, theme switch, account menu) and the vault
  // banner, whose readiness travels in `GET meta` (§3.4), and the tabs' badges (the Tools tab: the
  // queue to classify). The poll does not extend the session (BR-204).
  import { onMount } from 'svelte';
  import type { Administrator, Client, Meta } from '../lib/api';
  import { badges } from '../lib/badges.svelte';
  import { t } from '../lib/i18n';
  import { notices } from '../lib/notice.svelte';
  import { TABS, type Callback, type Tab, type SetupLanding } from '../lib/routes';
  import type { Theme } from '../lib/theme';
  import OAuthCallback from './OAuthCallback.svelte';
  import Passkeys from './Passkeys.svelte';
  import SetupGuide from './SetupGuide.svelte';

  let {
    client,
    admin,
    tab,
    landing = null,
    tabs = TABS,
    theme,
    callback = null,
    onCallbackDone = () => {},
    onTab,
    onTheme,
    onSignedOut,
  }: {
    client: Client;
    admin: Administrator;
    tab: string;
    landing?: SetupLanding | null;
    tabs?: readonly Tab[];
    theme: Theme;
    callback?: Callback | null;
    onCallbackDone?: () => void;
    onTab: (id: string) => void;
    onTheme: (theme: Theme) => void;
    onSignedOut: () => void;
  } = $props();

  const POLL_MS = 30_000;
  // the screen of the current tab is the one its entry carries (§3.17)
  const current = $derived(tabs.find((item) => item.id === tab));
  let meta = $state<Meta | null>(null);
  let menu = $state(false);
  let panel = $state<'passkeys' | null>(null);

  async function poll(): Promise<void> {
    // every read leaves at once, side by side: none waits for another's answer
    const reads = [
      client
        .call<Meta>('GET', 'meta', undefined, { activity: false })
        .then((read) => (meta = read))
        .catch(() => {
          // a 401 already took the console back to sign-in
        }),
      ...tabs.map((item) =>
        item.badge
          ? item
              .badge(client)
              .then((n) => (badges[item.id] = n))
              .catch(() => {
                // the count stays as last read; the screen shows the refusal when opened
              })
          : Promise.resolve(),
      ),
    ];
    await Promise.all(reads);
  }

  onMount(() => {
    void poll();
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  });

  async function signOut(): Promise<void> {
    try {
      await client.call('POST', 'session/signout');
    } finally {
      onSignedOut();
    }
  }
</script>

<header class="bar">
  <span class="wordmark">{t('wordmark')}</span>
  {#if meta?.organisation_name}<span>{meta.organisation_name}</span>{/if}
  <nav aria-label="Screens">
    {#each tabs as item (item.id)}
      <button
        type="button"
        class="tab"
        class:active={item.id === tab}
        aria-current={item.id === tab ? 'page' : undefined}
        aria-label={badges[item.id] ? t('tab.badge', { label: t(item.label), n: String(badges[item.id]) }) : undefined}
        onclick={() => onTab(item.id)}
        >{t(item.label)}{#if badges[item.id]}<span class="count" aria-hidden="true">{badges[item.id]}</span
          >{/if}</button
      >
    {/each}
  </nav>
  <span class="spacer"></span>
  <button
    type="button"
    class="ghost"
    onclick={() => onTheme(theme === 'dark' ? 'light' : 'dark')}
    >{theme === 'dark' ? t('shell.theme_light') : t('shell.theme_dark')}</button
  >
  <div class="account">
    <button type="button" class="ghost" aria-expanded={menu} onclick={() => (menu = !menu)}>
      {admin.email}
    </button>
    {#if menu}
      <ul class="menu" role="menu">
        <li role="none">
          <button type="button" role="menuitem" onclick={() => ((panel = 'passkeys'), (menu = false))}>
            {t('shell.passkeys')}
          </button>
        </li>
        <li role="none">
          <button type="button" role="menuitem" onclick={signOut}>{t('shell.sign_out')}</button>
        </li>
      </ul>
    {/if}
  </div>
</header>

{#if meta && !meta.readiness.ok}
  <div class="banner" role="status" data-state="SCR-03.vault-down">
    <strong>{t('shell.vault_down')}</strong> ({meta.readiness.reason}). {t('shell.vault_down_detail')}
  </div>
{/if}

{#if notices.tools}
  <div class="banner info" role="status" data-state="SCR-03.tools-notice">
    {t('shell.tools_notice')}
    <button type="button" class="ghost" onclick={() => (notices.tools = false)}>{t('dismiss')}</button>
  </div>
{/if}

<main class="content">
  <h1 class="sr">{t('shell.heading')}</h1>
  {#if callback}
    <OAuthCallback {client} {callback} onDone={onCallbackDone} />
  {:else if current}
    {#key tab}<SetupGuide {client} automatic />{/key}
    <current.screen {client} {landing} />
  {/if}
</main>

{#if panel === 'passkeys'}
  <Passkeys {client} onClose={() => (panel = null)} />
{/if}

<style>
  .bar {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-4);
    background: var(--surface-1);
    border-bottom: 1px solid var(--rule);
  }
  .wordmark {
    font-family: var(--font-mono);
    letter-spacing: 0.2em;
    color: var(--cyan);
    margin-right: var(--space-4);
  }
  nav {
    display: flex;
    gap: var(--space-1);
  }
  .tab {
    background: none;
    border: none;
    color: var(--muted);
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius);
  }
  .tab.active {
    color: var(--text);
    background: var(--hover);
  }
  .count {
    display: inline-block;
    margin-left: var(--space-1);
    min-width: 1.2rem;
    padding: 0 var(--space-1);
    border-radius: 999px;
    background: var(--warn-bg);
    color: var(--warn);
    border: 1px solid var(--warn-bd);
    font-size: 0.7rem;
    line-height: 1.4;
    text-align: center;
  }
  .spacer {
    flex: 1;
  }
  .account {
    position: relative;
  }
  .menu {
    position: absolute;
    right: 0;
    top: calc(100% + var(--space-1));
    list-style: none;
    margin: 0;
    padding: var(--space-1);
    background: var(--surface-2);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    min-width: 10rem;
    z-index: 10;
  }
  .menu button {
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    color: var(--text);
    padding: var(--space-2);
  }
  .menu button:hover {
    background: var(--hover);
  }
  .banner {
    background: var(--warn-bg);
    color: var(--warn);
    border-bottom: 1px solid var(--warn-bd);
    padding: var(--space-2) var(--space-4);
  }
  .banner.info {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    background: var(--info-bg);
    color: var(--text);
    border-bottom-color: var(--rule);
  }
  .content {
    padding: var(--space-3) var(--space-4);
  }
  @media (max-width: 600px) {
    .bar {
      flex-wrap: wrap;
      padding: var(--space-2);
    }
    .content {
      padding: var(--space-3) var(--space-2);
    }
  }
</style>
