// A small tree for the unit tests, in the server's node shape (`GET nodes`).
import type { Level, TreeNode } from './api';

export function node(id: string, path: string, level: Level, parent: string | null): TreeNode {
  return {
    id,
    path,
    parent,
    level,
    segment: path.split('/').pop() || '/',
    name: id,
    sensitive: false,
    marked: false,
    locked_by: null,
    locked_here: false,
    url: level === 'role' ? `https://mcp.example/o/k7mq2xv4tnpd/p/${path}/mcp` : null,
  };
}

export const NODES: TreeNode[] = [
  node('org', '/', 'org', null),
  node('acme', 'acme', 'client', 'org'),
  node('web', 'acme/web', 'project', 'acme'),
  node('prod', 'acme/web/prod', 'env', 'web'),
  node('ops', 'acme/web/prod/ops', 'role', 'prod'),
  node('dev', 'acme/web/prod/dev', 'role', 'prod'),
];
