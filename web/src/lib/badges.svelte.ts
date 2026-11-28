// The counts the shell shows on its tabs (§3.4: the Tools tab carries the size of the queue to
// classify). The shell's background poll writes them without extending the session (BR-204); a
// screen that has just read the same count writes it too, so the badge follows its acts at once.

export const badges = $state<Record<string, number>>({});
