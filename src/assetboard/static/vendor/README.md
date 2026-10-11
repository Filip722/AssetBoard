# Vendored frontend assets

These browser libraries are pinned and served locally. The application does not load them from a
CDN at runtime.

| Asset | Version | License | SHA-256 | License file | Source |
|---|---:|---|---|---|---|
| `htmx.min.js` | 2.0.4 | Zero-Clause BSD (0BSD) | `69caefd0da92269066e725d7fe175e26b9d50c962e3056459c0c477154cdb9d3` | `HTMX-LICENSE.txt` | https://github.com/bigskysoftware/htmx |
| `alpinejs-csp.min.js` | 3.14.8 | MIT | `c98f46291087f0f93c9cdefb682d120fe1d2b3dcb649de83cfc1ad5b1fac58e4` | `ALPINE-LICENSE.txt` | https://github.com/alpinejs/alpine |
| `echarts.min.js` | 5.6.0 | Apache-2.0 | `bf4a223524e40b77c304bec67e1222cf551f14880cf42c69dc046558e11c07b1` | `ECHARTS-LICENSE.txt` | https://github.com/apache/echarts |

### Build tools (not shipped)

| Asset | Version | License | SHA-256 | Source |
|---|---:|---|---|---|
| Tailwind CSS standalone CLI (Linux x86_64) | 3.4.17 | MIT | `7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4` | https://github.com/tailwindlabs/tailwindcss |
| Tailwind CSS standalone CLI (Linux ARM64) | 3.4.17 | MIT | `69b1378b8133192d7d2feb12a116fa12d035594f58db3eff215879e4ad8cf39b` | https://github.com/tailwindlabs/tailwindcss |
| Tailwind CSS standalone CLI (macOS x86_64) | 3.4.17 | MIT | `6cbdad74be776c087ffa5e9a057512c54898f9fe8828d3362212dfe32fc933a3` | https://github.com/tailwindlabs/tailwindcss |
| Tailwind CSS standalone CLI (macOS ARM64) | 3.4.17 | MIT | `a1d0c7985759accca0bf12e51ac1dcbf0f6cf2fffb62e6e0f62d091c477a10a3` | https://github.com/tailwindlabs/tailwindcss |

The Tailwind CLI checksums are pinned in the `css` Makefile target. It is fetched from its
versioned upstream release during `make css` and cached in
`.cache/`; it is a build tool and is not used by the running application. It is not included in
the shipped assets, so no standalone Tailwind license file is kept here.

Apache ECharts 5.6.0 ships an upstream `NOTICE`; its exact text is included in `ECHARTS-NOTICE.txt`.

### Bundled data (not served)

| File | Upstream revision | License | SHA-256 | License file | Source |
|---|---|---|---|---|---|
| `src/assetboard/data/common-passwords.txt` | SecLists `913b327317496d062bcc7cace524aaad8a693be2` | MIT | `fa1ea0cf2f673458b58be3a7171d2b7fc6f2e704b4691e69c13a9a8511f5cf31` | `src/assetboard/data/SECLists-LICENSE.txt` | https://github.com/danielmiessler/SecLists (`Passwords/Common-Credentials/10k-most-common.txt`) |

The password list is the first 10,000 lines of the upstream file at that revision, byte for byte;
the upstream file has 10,001 entries. Its source and license are recorded in
`src/assetboard/data/NOTICE`, which the container image ships under `/licenses/AssetBoard/`.
