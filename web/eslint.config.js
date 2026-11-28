import js from '@eslint/js';
import svelte from 'eslint-plugin-svelte';
import ts from 'typescript-eslint';

export default ts.config(
  { ignores: ['dist/', 'node_modules/'] },
  js.configs.recommended,
  ...ts.configs.recommended,
  ...svelte.configs.recommended,
  {
    files: ['**/*.svelte', '**/*.svelte.ts'],
    languageOptions: { parserOptions: { parser: ts.parser } },
  },
  {
    rules: {
      // TypeScript and svelte-check resolve names; ESLint's own check knows no DOM types
      'no-undef': 'off',
      // BR-212: the CSP forbids inline styles; dynamic styles go through CSSOM
      'svelte/no-inline-styles': 'error',
    },
  },
);
