# Changelog

## [0.2.1](https://github.com/LexanderPL/AssetBoard/compare/v0.2.0...v0.2.1) (2026-10-19)


### Features

* **auth:** lower the minimum password length to 8 ([#107](https://github.com/LexanderPL/AssetBoard/issues/107)) ([d83de3d](https://github.com/LexanderPL/AssetBoard/commit/d83de3d9d69a33cbd66b1a198a8e47adf901f198))
* **categories:** give every new workspace the suggested categories automatically ([#118](https://github.com/LexanderPL/AssetBoard/issues/118)) ([eb82693](https://github.com/LexanderPL/AssetBoard/commit/eb82693aeac70e309cdb322574293d3bb34aa568))
* **db:** log one line when startup migrations apply ([#104](https://github.com/LexanderPL/AssetBoard/issues/104)) ([1b3407c](https://github.com/LexanderPL/AssetBoard/commit/1b3407cd2802c309f5e790a99135759e131cd73d))
* **invitations:** require an email address for every new invitation ([#116](https://github.com/LexanderPL/AssetBoard/issues/116)) ([4d028ad](https://github.com/LexanderPL/AssetBoard/commit/4d028addd3c80ff64a0709f887af245394d2f86b))
* **ledger:** add budget lines (planned values), plan copy and the auto-copy setting (F2.3 part A) ([#117](https://github.com/LexanderPL/AssetBoard/issues/117)) ([d7f0ec9](https://github.com/LexanderPL/AssetBoard/commit/d7f0ec9947373f29491d4922a2c00e282d6e0ac3))
* **ledger:** add categories with inline edit, reorder, archive and seed set (F2.1) ([#108](https://github.com/LexanderPL/AssetBoard/issues/108)) ([166b010](https://github.com/LexanderPL/AssetBoard/commit/166b010ba6f3be7ac27cc9e01d3c695644498606))
* **ledger:** add month pages with navigation, closing and the closed-month guard (F2.5) ([#109](https://github.com/LexanderPL/AssetBoard/issues/109)) ([403ff2d](https://github.com/LexanderPL/AssetBoard/commit/403ff2d96014409abf0a05a29c0d5109110bc39a))
* **ledger:** add month-page transaction entry with fast keyboard entry, autocomplete and undo (F2.2 part B) ([#115](https://github.com/LexanderPL/AssetBoard/issues/115)) ([750c18b](https://github.com/LexanderPL/AssetBoard/commit/750c18b586b9b685fa099444f2b0878102c61419))
* **ledger:** add the transactions model and service, base-currency lock and closed-month warning (F2.2 part A) ([#113](https://github.com/LexanderPL/AssetBoard/issues/113)) ([f0339d1](https://github.com/LexanderPL/AssetBoard/commit/f0339d1b35306a59025e11263063e4eca3ea1ef6))
* **ledger:** edit planned values in the month summary, copy the plan, auto-copy on open (F2.3 part B) ([#121](https://github.com/LexanderPL/AssetBoard/issues/121)) ([e35b4f5](https://github.com/LexanderPL/AssetBoard/commit/e35b4f5425b0b5ec49f68d9d2ed49e99ef815717))
* **ledger:** filter the month's transactions by a summary category (F2.4 part B) ([#125](https://github.com/LexanderPL/AssetBoard/issues/125)) ([a8846df](https://github.com/LexanderPL/AssetBoard/commit/a8846dfecb5f837a6f52c613f2f20783e4c15960))
* **ledger:** month summary with actuals, differences and KPIs (F2.4 part A) ([#124](https://github.com/LexanderPL/AssetBoard/issues/124)) ([ad786a8](https://github.com/LexanderPL/AssetBoard/commit/ad786a8c8f44f310a6c9afacaa47570e3c44b775))
* **ledger:** transaction search, CSV export and bulk recategorize (F2.6) ([#126](https://github.com/LexanderPL/AssetBoard/issues/126)) ([0f8ce10](https://github.com/LexanderPL/AssetBoard/commit/0f8ce1089fa1e73dc621b8b3735fb983148f61eb))
* **ui,auth:** neutral colour tokens, fixed select arrows, and enumeration-safe separate sign-in messages ([#114](https://github.com/LexanderPL/AssetBoard/issues/114)) ([fce2606](https://github.com/LexanderPL/AssetBoard/commit/fce260668ad2cbe88b719842b505b973b4a332eb))
* **workspaces:** self-service workspace creation with an instance setting (pre-release A3) ([#123](https://github.com/LexanderPL/AssetBoard/issues/123)) ([0f9156a](https://github.com/LexanderPL/AssetBoard/commit/0f9156a09629d885780e59ecc97b20e42ae3fd8e))


### Bug Fixes

* **ui:** settle HTMX swaps at once so new rows are wired and focused before input ([#119](https://github.com/LexanderPL/AssetBoard/issues/119)) ([8c59902](https://github.com/LexanderPL/AssetBoard/commit/8c59902418eebb901de9c52f08475055ac311eeb))


### Documentation

* **specs:** replace the Excel importer with a workspace archive for export, import and restore (M4) ([#120](https://github.com/LexanderPL/AssetBoard/issues/120)) ([e4cbebe](https://github.com/LexanderPL/AssetBoard/commit/e4cbebe2ba2ea8b0ea93c31318a531450cc0692e))
* write up the post-1.0 backlog and reword the published-image intro ([#105](https://github.com/LexanderPL/AssetBoard/issues/105)) ([d7413f4](https://github.com/LexanderPL/AssetBoard/commit/d7413f440022f4f43a4ff123a5465662dd8d1c07))

## [0.2.0](https://github.com/LexanderPL/AssetBoard/compare/v0.1.0...v0.2.0) (2026-10-14)


### Features

* **auth:** add instance administration of user accounts (F1.5 part B) ([#100](https://github.com/LexanderPL/AssetBoard/issues/100)) ([0593062](https://github.com/LexanderPL/AssetBoard/commit/05930624e7beb0037c2ec323fbd057bf1d6da8d3))
* **auth:** add OIDC client, account mapping and password-confirmed linking (F1.3 part A) ([785e15e](https://github.com/LexanderPL/AssetBoard/commit/785e15e9e58a94618323c8c003c6750c97166c3b))
* **auth:** add OIDC sign-in pages, break-glass login and provider logout (F1.3 part B) ([#96](https://github.com/LexanderPL/AssetBoard/issues/96)) ([634593f](https://github.com/LexanderPL/AssetBoard/commit/634593f9919b5c0826b0dccb59305d4b31182d01))
* **auth:** add proxy auth settings, TCP peer capture and shared account mapping (F1.4 part A) ([#97](https://github.com/LexanderPL/AssetBoard/issues/97)) ([3b12b56](https://github.com/LexanderPL/AssetBoard/commit/3b12b56f1aac9fb1c350a318a128d7a19979fe26))
* **auth:** add proxy sign-in routes, proxy-bound sessions and proxy-auth guide (F1.4 part B) ([#98](https://github.com/LexanderPL/AssetBoard/issues/98)) ([3fcb2e8](https://github.com/LexanderPL/AssetBoard/commit/3fcb2e85e8a863499f3a177a16f3b2dd618a660a))
* **auth:** add setup, sign-in and account pages with CSRF and access gate (F1.1 part B2) ([fcf6814](https://github.com/LexanderPL/AssetBoard/commit/fcf6814c1f4ee663d998a48df9f9cab5b3a63b87))
* **auth:** add the audit log page (F1.5 part C) ([#101](https://github.com/LexanderPL/AssetBoard/issues/101)) ([05cf5dc](https://github.com/LexanderPL/AssetBoard/commit/05cf5dca492b54c05672287e87e18e57e124e8ae))
* **auth:** add the profile page with linked sign-in methods (F1.5 part A) ([#99](https://github.com/LexanderPL/AssetBoard/issues/99)) ([d09a700](https://github.com/LexanderPL/AssetBoard/commit/d09a700dc8f8645e5a2a756dc3b38700cf03867c))
* **auth:** add throttle reservations, hash concurrency cap and CSRF primitives (F1.1 part B1) ([2b79af5](https://github.com/LexanderPL/AssetBoard/commit/2b79af5338122d5ba8698dde7b6174d0f41961f8))
* **auth:** F1.1 PR A — identity models, local auth services and admin CLI ([698ee69](https://github.com/LexanderPL/AssetBoard/commit/698ee69654c2ca265803c36a2192a1250ec691fd))
* **workspaces:** add request Context, role guards, scoped repositories and tenant-isolation test (F1.2 part A) ([#90](https://github.com/LexanderPL/AssetBoard/issues/90)) ([6fbc55f](https://github.com/LexanderPL/AssetBoard/commit/6fbc55f763678b3d44e89f132590aa2638a550ff))
* **workspaces:** add single-use invitation links with email lock and invitation-only sign-up (F1.2 part B2) ([#92](https://github.com/LexanderPL/AssetBoard/issues/92)) ([9aff5f3](https://github.com/LexanderPL/AssetBoard/commit/9aff5f39802ef75721d6a17fc9eb38df6a9f75af))
* **workspaces:** add workspace switcher, settings and members pages with last-owner rule (F1.2 part B1) ([#91](https://github.com/LexanderPL/AssetBoard/issues/91)) ([bb1344f](https://github.com/LexanderPL/AssetBoard/commit/bb1344f182cafd4406d624409b91307403a429d1))


### Bug Fixes

* **web:** send no Referer on subresources of pages that can carry an invitation token ([10a844d](https://github.com/LexanderPL/AssetBoard/commit/10a844dd744111d68253942916dd2d4e227ab04f))


### Documentation

* add the 0.1.0 to 0.2.0 upgrade guide ([#102](https://github.com/LexanderPL/AssetBoard/issues/102)) ([13353f9](https://github.com/LexanderPL/AssetBoard/commit/13353f9bcaf43a95dd74e35e6e925bf20fe43ac5))
* **specs:** move F1.6 TOTP to the post-1.0 backlog ([a06b74b](https://github.com/LexanderPL/AssetBoard/commit/a06b74bc5b41d22309bb7fe85003f5356b4362ac))

## 0.1.0 (2026-10-10)


### Features

* add application foundation and configuration ([54c1276](https://github.com/LexanderPL/AssetBoard/commit/54c12769619be59910f98ad3ba992c0a80f792cf))
* add database foundation and money types ([a126310](https://github.com/LexanderPL/AssetBoard/commit/a12631057086bda5d529049c541c33e1433c9e8f))
* add Docker deployment and release automation ([#6](https://github.com/LexanderPL/AssetBoard/issues/6)) ([d2ecb13](https://github.com/LexanderPL/AssetBoard/commit/d2ecb13f63016980427ca579f0a5c193a59d6071))
* add repository scaffold and tooling ([#1](https://github.com/LexanderPL/AssetBoard/issues/1)) ([a7894f0](https://github.com/LexanderPL/AssetBoard/commit/a7894f024476067e376239920138fa4b3172911b))
* **web:** implement F0.4 UI shell, i18n, and dev server ([e69e47c](https://github.com/LexanderPL/AssetBoard/commit/e69e47c87c334242275b656a92c7cd11ef127d87))


### Documentation

* Add specs ([956dd24](https://github.com/LexanderPL/AssetBoard/commit/956dd24420046207c00488b0b5d029b2679eee71))
* refine F0.6 governance and deployment documentation ([ef1336e](https://github.com/LexanderPL/AssetBoard/commit/ef1336e8a48466dd81860bd9e3530600cba09d26))
