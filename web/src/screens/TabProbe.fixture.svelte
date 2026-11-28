<script lang="ts">
  // Test fixture of Shell.test.ts: a screen that is in no slice, added to the tabs by one entry.
  import type { Client } from '../lib/api';

  let { client }: { client: Client } = $props();
</script>

<p data-probe="stats">probe screen {client ? 'with its client' : ''}</p>
