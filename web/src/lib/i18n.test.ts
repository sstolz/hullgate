import { describe, expect, it } from 'vitest';
import { codeText, en, impactKey, reasonText, t } from './i18n';
import { stored } from './theme';

const values = Object.values(en).join('\n');
const lower = values.toLowerCase();

// AC-20: each correction of §3.2's table is in the dictionary, and the former strings are absent.
describe('dictionary (§3.2, AC-20)', () => {
  it('names the product HULLGATE, never the former name', () => {
    expect(en.wordmark).toBe('HULLGATE');
    // built from fragments: no tracked file spells the former name (HULL-58)
    expect(lower).not.toContain('cloi' + 'sonn');
    expect(lower).not.toContain('monolith');
  });

  it('says "locked" for a locked node, never "frozen"', () => {
    expect(en['badge.locked']).toBe('locked');
    expect(en['tree.locked_by']).toBe('Locked by {path}');
    expect(en['lock.by']).toContain('Locked by');
    expect(lower).not.toContain('frozen');
    expect(lower).not.toContain('freeze');
  });

  it('calls tool definitions "pinned"', () => {
    expect(en['diag.version']).toContain('pinned definitions');
    expect(en['tools.pinned_title']).toBe('Pinned definitions');
    expect(en['tools.pinned_intro']).toContain('A pinned tool whose definition changes upstream is suspended');
    expect(en['rights.pinned']).toBe('pinned');
    expect(en['policy.pin']).toBe('Pin definitions');
    expect(en['reason.suspended']).toContain('pinned definition');
  });

  it("writes the Tools intro on F-015's classification rule (BR-171, BR-173, D5)", () => {
    const intro = en['tools.intro'];
    expect(intro).toContain(
      'manual classification, otherwise what its MCP annotations declare, otherwise destructive',
    );
    expect(intro).toContain('An unclassified tool is therefore destructive');
    expect(intro).toContain('only under the destructive mode, or when the tool is opened');
    expect(intro).toContain('the queue itself changes no exposure');
  });

  it("drops the prototype's slice 3 strings that F-015 and BR-051 correct", () => {
    // `tools_intro`, `r_unclassified`, `tip_frozen`, `tip_grouped` and `test_sens` of I18N.en
    expect(values).not.toContain('hidden on every role until it is classified');
    expect(values).not.toContain('hidden until classified');
    expect(values).not.toContain('Every catalog tool carries an annotation');
    expect(values).not.toContain('takes the strongest annotation');
    expect(values).not.toContain('Only the node that froze it can unfreeze');
    expect(values).not.toMatch(/dedicated token on \{env\} or below/);
    expect(en['reach.sensitive_requires_role_token']).toBe(
      'a sensitive role accepts only a token issued on this role',
    );
  });

  it('says a sensitive role accepts only a token issued on this role', () => {
    expect(en['tokens.hint_sensitive']).toBe(
      'Sensitive environment: only a token issued on this role is accepted.',
    );
    expect(values).not.toContain('or below is accepted');
    expect(values).not.toMatch(/issued on \{env\}/);
  });

  it('keeps name, prefix and placements on revoke; Remove is a separate act', () => {
    const revoke = en['conn.revoke_body'];
    expect(revoke).toContain('keeps its name, prefix and placements');
    expect(revoke).toContain('Remove is a separate act');
    expect(revoke.toLowerCase()).not.toContain('placements removed');
    expect(revoke.toLowerCase()).not.toContain('placement(s) removed');
    expect(en['conn.remove']).toBe('Remove');
    expect(en['impact.n.placements']).not.toMatch(/removed/i);
  });

  it('drops the unclassified-tool sentence and the Statistics tab', () => {
    expect(values).not.toContain('An unclassified tool is hidden on every role');
    expect(lower).not.toContain('statistics');
    expect(Object.keys(en).filter((k) => k.startsWith('tab.') && k !== 'tab.badge')).toEqual([
      'tab.import',
      'tab.organisation',
      'tab.tree',
      'tab.tokens',
      'tab.tools',
      'tab.audit',
    ]);
  });

  it('no longer holds the lot-A placeholder of the tree', () => {
    expect(lower).not.toContain('arrive with the next lot');
  });

  it('says when connected clients see a change (BR-215)', () => {
    expect(en['shell.tools_notice']).toBe(
      'Connected clients see the new list at their next tools/list.',
    );
  });

  it('says neighbours of a shared connection share its call limit (BR-079)', () => {
    expect(en['share.neighbours']).toContain('share its call limit');
  });

  it('is English only, one dictionary', () => {
    expect(values).not.toMatch(/[éèàùç]/);
  });
});

describe('t', () => {
  it('fills placeholders and leaves unknown ones visible', () => {
    expect(t('tree.add_child', { type: 'role', name: 'dev' })).toBe('Add a role under dev');
    expect(t('tree.delete')).toBe('Delete {name}');
  });

  it('names known impact numbers and leaves the others to the raw key', () => {
    expect(impactKey('tokens_revoked')).toBe('impact.n.tokens_revoked');
    expect(impactKey('something_new')).toBeNull();
    expect(reasonText('other_client')).toBe(en['share.other_client']);
    expect(reasonText('new_reason')).toBe('new_reason');
  });

  it('names the closed codes of slice 3 and shows an unknown one as sent', () => {
    expect(codeText('reason', 'above_mode')).toBe(en['reason.above_mode']);
    expect(codeText('reach', 'revoked')).toBe('token revoked');
    expect(codeText('class', 'destructive')).toBe('destructive');
    expect(codeText('reason', 'brand_new')).toBe('brand_new');
  });
});

describe('theme', () => {
  it('is dark by default and survives a storage that throws', () => {
    expect(stored(undefined)).toBe('dark');
    const throwing = {
      getItem: () => {
        throw new Error('blocked');
      },
    };
    expect(stored(throwing)).toBe('dark');
    expect(stored({ getItem: () => 'light' })).toBe('light');
  });
});


it('explains every undo refusal in human text', () => {
  const reasons = ['part_of_import', 'no_change', 'deleted_node_never_returns',
    'cut_lineages_do_not_return', 'credential_cannot_be_restored', 'not_reversible',
    'history_snapshot_unavailable'];
  for (const reason of reasons) {
    expect(codeText('undo', reason)).not.toBe(reason);
    expect(codeText('undo', reason)).not.toContain('_');
  }
});
