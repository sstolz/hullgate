// BR-215's notice, shown by the shell after a write whose answer carries `tools_notice: true`:
// connected clients see the new list at their next `tools/list` (the gateway pushes nothing).

import { toolsNotice } from './api';

export const notices = $state<{ tools: boolean }>({ tools: false });

/** Records what a write's answer asks the console to say, and returns the answer. */
export function afterWrite<T>(answer: T): T {
  if (toolsNotice(answer)) notices.tools = true;
  return answer;
}
