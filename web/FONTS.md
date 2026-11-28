# Console fonts (F-016 §8, BR-212)

Self-hosted: the build bundles the WOFF2 and WOFF files of these packages into `dist/assets/`, so
the console requests no font from another origin (`font-src 'self'`). `npm run build` copies each
package's licence beside them, `dist/assets/LICENSE-inter.txt` and
`dist/assets/LICENSE-jetbrains-mono.txt` (OFL 1.1 condition 2), so the image's `/app/console/assets/`
ships the licence text with the fonts.

| Font | Package (`package-lock.json`) | Licence |
|---|---|---|
| Inter | `@fontsource/inter`, latin subset, weights 400 and 600 | SIL Open Font License 1.1 (`node_modules/@fontsource/inter/LICENSE`) |
| JetBrains Mono | `@fontsource/jetbrains-mono`, latin subset, weight 400 | SIL Open Font License 1.1 (`node_modules/@fontsource/jetbrains-mono/LICENSE`) |

Inter Tight and Space Grotesk, named by §8 as the prototype's other substitutes, are added with the
screens that use them. TWK Lausanne and Neo Sans Pro are not used, and no file of
`design/amaterasu/` is (#41).
