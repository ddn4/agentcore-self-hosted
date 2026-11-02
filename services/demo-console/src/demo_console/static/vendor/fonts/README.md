# Vendored fonts

The console loads no fonts from the network. Both families are under the SIL Open Font
License 1.1 (licence texts beside them). The files are the unmodified Latin subsets that
Google Fonts serves (`fonts.gstatic.com`), downloaded 2026-11-02.

| File | Family | Source | Licence |
|---|---|---|---|
| `hanken-grotesk-latin-wght.woff2` | Hanken Grotesk, variable `wght` 100–900 | Google Fonts `hankengrotesk/v12`; upstream github.com/marcologous/hanken-grotesk | `OFL-HankenGrotesk.txt` |
| `ibm-plex-mono-latin-400.woff2` | IBM Plex Mono Regular | Google Fonts `ibmplexmono/v20`; upstream github.com/IBM/plex | `OFL-IBMPlexMono.txt` |
| `ibm-plex-mono-latin-500.woff2` | IBM Plex Mono Medium | as above | `OFL-IBMPlexMono.txt` |

Hanken Grotesk stands in for Aeonik, the commercial typeface on temporal.io, which is not
copied or linked (docs/console/BRAND.md). "Plex" is a Reserved Font Name: these files are
redistributed unmodified, so they keep the name. Characters outside the Latin subset fall
back to the system font.

SHA-256:

```
1f21c6eaa0000f3329cfcfac966b43d5bebf5aa610303e33294ac31bc6f4bb59  hanken-grotesk-latin-wght.woff2
c36f509c0a8f9f85f29cb44bc8701d8a9e0b14c499e77a884f789ead7093a7ac  ibm-plex-mono-latin-400.woff2
a76f53ca6612e7b3828eec2311098675b7f9849ae4169a8bcef6302aec02a6c0  ibm-plex-mono-latin-500.woff2
```
