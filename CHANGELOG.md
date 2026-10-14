# Changelog

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
