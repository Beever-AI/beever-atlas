import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    rules: {
      'react-hooks/set-state-in-effect': 'warn',
      'react-hooks/static-components': 'warn',
      'react-hooks/purity': 'warn',
      // ``react-hooks/refs`` from the React 19 strict ruleset bans
      // reading/writing ``ref.current`` during render. Several pre-
      // existing components in this repo (notably ``SyncProgressV2``)
      // intentionally read+reset refs during render to avoid a one-
      // frame stale window that ``useEffect``-based resets would
      // introduce. Demoted to ``warn`` so CI doesn't block on this
      // pattern; team can refactor each call site case-by-case.
      'react-hooks/refs': 'warn',
      // ``react-hooks/preserve-manual-memoization`` fires when the
      // React Compiler bails on a component (typically because of the
      // same ref-during-render pattern flagged by ``refs``). Demoted
      // for the same reason — the manual ``useMemo`` calls in
      // ``SyncProgressV2`` are correct and intentional.
      'react-hooks/preserve-manual-memoization': 'warn',
      'react-refresh/only-export-components': 'warn',
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      'no-useless-escape': 'warn',
      'no-misleading-character-class': 'warn',
    },
  },
])
