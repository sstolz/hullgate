<script lang="ts">
  // The policy of one placement (SCR-05), F-015's `policy` verbs: mode, the lists open, deny and
  // allowlist, argument rules, the placement's pin and pinned tools. Every write answers the new
  // policy object, which replaces the one shown; refusals are the server's text (BR-211), a locked
  // subtree included (BR-049). Suggested tool names come from the role's preview; the server
  // judges every name (BR-176).
  import { message, type Client } from '../lib/api';
  import { t } from '../lib/i18n';
  import { afterWrite } from '../lib/notice.svelte';
  import {
    entryName,
    LISTS,
    MODES,
    modeChoice,
    parseValues,
    PINS,
    ruleName,
    type ListName,
    type PlacementPolicy,
  } from '../lib/policy';

  let {
    client,
    policy,
    names = [],
    readOnly = false,
    lockedBy = null,
    onChange,
  }: {
    client: Client;
    policy: PlacementPolicy;
    names?: readonly string[];
    readOnly?: boolean;
    lockedBy?: string | null;
    onChange: (p: PlacementPolicy) => void;
  } = $props();

  const uid = Math.random().toString(36).slice(2, 9);
  const listId = `names-${uid}`;
  let adding = $state<Record<ListName, string>>({ open: '', deny: '', allowlist: '' });
  let rule = $state({ tool: '', argument: '', values: '' });
  let pinTool = $state('');
  let error = $state('');
  let done = $state('');
  let busy = $state(false);

  const base = $derived({ connection: policy.connection, role: policy.role });
  const rules = $derived(Object.entries(policy.arguments));

  async function write(method: string, path: string, body: Record<string, unknown>): Promise<boolean> {
    // controls stay enabled while a write runs, so that the focus stays put (BR-232)
    if (busy) return false;
    busy = true;
    error = '';
    done = '';
    try {
      const answer = afterWrite(
        await client.call<PlacementPolicy & { already?: boolean }>(method, path, { ...base, ...body }),
      );
      done = answer.already ? t('policy.already') : t('policy.saved');
      onChange(answer);
      return true;
    } catch (e) {
      error = message(e);
      return false;
    } finally {
      busy = false;
    }
  }

  async function addTo(list: ListName, event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const tool = adding[list].trim();
    if (!tool) return;
    if (await write('POST', 'placements/lists', { list, tools: [tool] })) adding[list] = '';
  }

  async function setRule(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const ok = await write('PUT', 'placements/arguments', {
      tool: rule.tool.trim(),
      argument: rule.argument.trim(),
      values: parseValues(rule.values),
    });
    if (ok) rule = { tool: '', argument: '', values: '' };
  }

  async function pin(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const tool = pinTool.trim();
    if (!tool) return;
    if (await write('POST', 'placements/tool-pins', { tool })) pinTool = '';
  }
</script>

<div class="policy" data-state="SCR-05.policy" aria-label={t('policy.title', { name: policy.connection })} role="group">
  <datalist id={listId}>
    {#each names as name (name)}<option value={name}></option>{/each}
  </datalist>
  {#if lockedBy}<p class="locked">{t('policy.read_only', { path: lockedBy })}</p>{/if}
  {#if policy.sensitive}<p class="hint sens">{t('policy.sensitive')}</p>{/if}

  <div class="row">
    <label>
      {t('policy.mode')}
      <select
        value={modeChoice(policy)}
        disabled={readOnly}
        onchange={(e) => void write('PUT', 'placements/mode', { mode: e.currentTarget.value })}
      >
        {#each MODES as mode (mode)}<option value={mode}>{mode === 'inherit' ? t('policy.inherit') : mode}</option>{/each}
      </select>
    </label>
    <span class="hint">
      {policy.mode} ({policy.mode_origin === 'placement'
        ? t('policy.origin_placement')
        : t('policy.origin_inherited', { path: policy.mode_origin })}) · <code>{policy.mode_origin}</code>
    </span>
    <label>
      {t('policy.pin')}
      <select
        value={policy.pin}
        disabled={readOnly}
        onchange={(e) => void write('PUT', 'placements/pin', { pin: e.currentTarget.value })}
      >
        {#each PINS as p (p)}
          <option value={p}>{p === 'on' ? t('policy.pin_on') : p === 'off' ? t('policy.pin_off') : t('policy.inherit')}</option>
        {/each}
      </select>
    </label>
  </div>

  {#each LISTS as list (list)}
    <fieldset>
      <legend>{t(`policy.list.${list}`)}</legend>
      {#if policy[list].length === 0}
        <span class="hint">{t('policy.list_empty')}</span>
      {:else}
        <ul class="chips">
          {#each policy[list] as tool, i (entryName(policy, list, i))}
            <li>
              <span class="mono">{tool}</span>
              {#if !readOnly}
                <button
                  type="button"
                  class="ghost small"
                  aria-label={t('policy.remove_from', { tool, list })}
                  onclick={() =>
                    write('POST', 'placements/lists/remove', { list, tools: [entryName(policy, list, i)] })}
                  >×</button
                >
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
      {#if !readOnly}
        <form class="add" onsubmit={(e) => addTo(list, e)}>
          <input
            aria-label={`${t('policy.tool_name')} · ${list}`}
            placeholder={t('policy.tool_name')}
            list={listId}
            bind:value={adding[list]}
          />
          <button type="submit" class="ghost small" disabled={!adding[list].trim()}>{t('policy.add')}</button>
          {#if list === 'allowlist'}
            <button
              type="button"
              class="ghost small"
              onclick={() => write('POST', 'placements/lists', { list: 'allowlist', class: 'read' })}
              >{t('policy.allow_reads')}</button
            >
          {/if}
        </form>
      {/if}
    </fieldset>
  {/each}

  <fieldset>
    <legend>{t('policy.arguments')}</legend>
    {#if rules.length === 0}
      <span class="hint">{t('policy.no_arguments')}</span>
    {:else}
      <ul class="chips">
        {#each rules as [tool, r] (tool)}
          <li>
            <span class="mono">{tool}</span>
            <span class="hint">{r.argument} ∈ {r.values.join(', ')}</span>
            {#if !readOnly}
              <button
                type="button"
                class="ghost small"
                aria-label={t('policy.arg_remove', { tool })}
                onclick={() => write('POST', 'placements/arguments/remove', { tool: ruleName(policy, tool) })}
                >×</button
              >
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
    {#if !readOnly}
      <form class="add rule" onsubmit={setRule}>
        <input aria-label={t('policy.arg_tool')} placeholder={t('policy.arg_tool')} list={listId} bind:value={rule.tool} />
        <input aria-label={t('policy.arg_argument')} placeholder={t('policy.arg_argument')} bind:value={rule.argument} />
        <input aria-label={t('policy.arg_values')} placeholder={t('policy.arg_values')} bind:value={rule.values} />
        <button
          type="submit"
          class="ghost small"
          disabled={!rule.tool.trim() || !rule.argument.trim()}>{t('policy.arg_set')}</button
        >
      </form>
    {/if}
  </fieldset>

  <fieldset>
    <legend>{t('policy.tool_pins')}</legend>
    {#if policy.pinned_tools.length === 0}
      <span class="hint">{t('policy.no_tool_pins')}</span>
    {:else}
      <ul class="chips">
        {#each policy.pinned_tools as tool, i (entryName(policy, 'pinned_tools', i))}
          <li>
            <span class="mono">{tool}</span>
            {#if !readOnly}
              <button
                type="button"
                class="ghost small"
                aria-label={t('policy.unpin_tool', { tool })}
                onclick={() =>
                  write('POST', 'placements/tool-pins/remove', { tool: entryName(policy, 'pinned_tools', i) })}
                >×</button
              >
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
    {#if !readOnly}
      <form class="add" onsubmit={pin}>
        <input aria-label={t('policy.pin_tool')} placeholder={t('policy.tool_name')} list={listId} bind:value={pinTool} />
        <button type="submit" class="ghost small" disabled={!pinTool.trim()}>{t('policy.pin_tool')}</button>
      </form>
    {/if}
  </fieldset>

  {#if done}<p class="ok" role="status">{done}</p>{/if}
  {#if error}<p class="notice" role="alert">{error}</p>{/if}
</div>

<style>
  .policy {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    padding: var(--space-2);
    margin: var(--space-1) 0 var(--space-2);
    background: var(--surface-2);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    width: 100%;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2) var(--space-3);
    align-items: center;
  }
  label {
    display: flex;
    gap: var(--space-1);
    align-items: center;
  }
  fieldset {
    margin: 0;
    padding: var(--space-1) var(--space-2) var(--space-2);
  }
  .chips {
    list-style: none;
    margin: 0 0 var(--space-1);
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }
  .chips li {
    display: inline-flex;
    gap: var(--space-1);
    align-items: center;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    padding: 0 0 0 var(--space-1);
  }
  .add {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
    align-items: center;
  }
  .add input {
    flex: 1;
    min-width: 8rem;
  }
  .locked {
    color: var(--warn);
    margin: 0;
  }
  .sens {
    color: var(--warn);
    margin: 0;
  }
  .ok,
  .notice {
    margin: 0;
  }
</style>
