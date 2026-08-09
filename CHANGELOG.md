# Changelog

## [0.1.1](https://github.com/rajeev-chaurasia/acquit/compare/v0.1.0...v0.1.1) (2026-08-09)


### Bug Fixes

* resolve the release-please merge conflict, keep v5 and the dispatch step ([27a33ce](https://github.com/rajeev-chaurasia/acquit/commit/27a33ce7e4d5d0dd5e4511f5d788905b9cb690ad))
* unique Marketplace display name, the bare word collides ([72d6b16](https://github.com/rajeev-chaurasia/acquit/commit/72d6b16633b31de9c7f31e17d09e105dc92a6b54))


### Documentation

* pin the quickstart to v0.1.0 ([f3da1f0](https://github.com/rajeev-chaurasia/acquit/commit/f3da1f0f9cacf24cd901dd6f4a8def151f16a90c))

## [0.1.0](https://github.com/rajeev-chaurasia/acquit/compare/v0.0.1...v0.1.0) (2026-08-09)


### Features

* canary mode validates selections live without skipping ([cec4512](https://github.com/rajeev-chaurasia/acquit/commit/cec4512525888f7d0d2b2aa44771b8eace0ef6c0))
* detect syspath_prepend and syspathinsert as sys.path mutations ([4d6a5ea](https://github.com/rajeev-chaurasia/acquit/commit/4d6a5eafa9d016302248d2e47fa0860b81263fbd))
* **fixtures:** narrowing scenarios, selfhost enablement, ADR 0008 accepted ([847f156](https://github.com/rajeev-chaurasia/acquit/commit/847f156f0eed4459e43091c69e4b1415feb53121))
* **folding:** constant-fold provable dynamic imports into edges (ADR 0009) ([0e8817d](https://github.com/rajeev-chaurasia/acquit/commit/0e8817d98811ce9b0c661c0f64f5a3351cbc436f))
* **graph:** annotate pure re-exporter inits with INIT_REEXPORT edges ([b5a049d](https://github.com/rajeev-chaurasia/acquit/commit/b5a049d6c1d4080e038a801bb513cc598d8c175f))
* **graph:** resolver seam and re-export narrowing checkers ([fe75975](https://github.com/rajeev-chaurasia/acquit/commit/fe7597528af8f5a966a3ddbfe1e595db6f32511c))
* OSS idiom census ranking standing hazards across repos ([d114d13](https://github.com/rajeev-chaurasia/acquit/commit/d114d13c670c18d48600505bfca7162f259fab71))
* **replay:** re-derive narrowed witnesses from both snapshots ([76e1def](https://github.com/rajeev-chaurasia/acquit/commit/76e1defbd663d0e730d76c1133931803be0b85fc))
* **select:** narrowed impact rule and witness claim for re-export narrowing ([1f978ca](https://github.com/rajeev-chaurasia/acquit/commit/1f978ca725d596ca8956c336d2c66bec3f892ad8))
* **study:** add suite_deps to manifests and refreeze constraints ([2c9a24e](https://github.com/rajeev-chaurasia/acquit/commit/2c9a24e5f70cd2c46021db989b945cb09926a0d3))
* **study:** count run-alls recoverable with assume_inert ([993ec9b](https://github.com/rajeev-chaurasia/acquit/commit/993ec9bc9d6553da5e38896f3eaf2c5cd430981b))
* **study:** install manifest suite_deps into every suite venv ([a998b50](https://github.com/rajeev-chaurasia/acquit/commit/a998b50904431eeb09ef9253fc9241077cbd56e1))
* **study:** mutation-injection arm ([715aaf7](https://github.com/rajeev-chaurasia/acquit/commit/715aaf7b281e1bc3c7329b82aad4a2b06ef4c076))
* **study:** narrowing arm with config injection and restricted mutant targets ([237285b](https://github.com/rajeev-chaurasia/acquit/commit/237285bf04b0b77c4f938eb375eb8334fae3bcf4))
* **study:** narrowing input on the study workflow and README docs ([fa3f2d2](https://github.com/rajeev-chaurasia/acquit/commit/fa3f2d21fdbd47e5801408e9a425cba6857b0f6c))


### Bug Fixes

* git failures carry R016 instead of the R018 catch-all ([13b44f4](https://github.com/rajeev-chaurasia/acquit/commit/13b44f4ae1e7e097d6d2ef2b2a75ba6ba5d47609))
* keep select's own output documents out of the tree fingerprint ([cbc3889](https://github.com/rajeev-chaurasia/acquit/commit/cbc3889fc24b2147d97216dcf4847ad9537c2301))
* **narrowing:** non-inert-observer condition closes the NARROW findings ([5beba93](https://github.com/rajeev-chaurasia/acquit/commit/5beba932e4bf4b77324b94773aa4471f8e49b0a6))
* scope sys.path mutation by when it executes ([8d52734](https://github.com/rajeev-chaurasia/acquit/commit/8d5273431d733ca6c4b1759b935fd6fc00963ea5))


### Documentation

* census results for the 61-repo corpus ([7af76dc](https://github.com/rajeev-chaurasia/acquit/commit/7af76dc0da2eb2c2aeeb203f408d0cffe3e64ad8))
* complete the documentation set for launch ([676e016](https://github.com/rajeev-chaurasia/acquit/commit/676e016bf1186800f5b12e7c9d35661e39bf1f00))
* derived registries measured, recommendation ([fc4c865](https://github.com/rajeev-chaurasia/acquit/commit/fc4c865cdb05386baf5c00c84c2b34062b3231b1))
* **folding:** ADR 0009 accepted, folding fixture repo, R007 and A1 notes ([361f80a](https://github.com/rajeev-chaurasia/acquit/commit/361f80af7b91d3aaa6a7c848c60d0636b91d4001))
* launch writeup and comparison ([859c66f](https://github.com/rajeev-chaurasia/acquit/commit/859c66ffe14029e2492e8a9147b121554c41af3a))
* **narrowing:** condition 7 revision, observer fixture, re-measured rates ([45e44a3](https://github.com/rajeev-chaurasia/acquit/commit/45e44a35dfad6a687f515f43123372ad86f90321))
* pasteable quickstart, local usage, and study provenance notes ([3876e0e](https://github.com/rajeev-chaurasia/acquit/commit/3876e0e0dfd7f5042e2ef34965c5ceda2d0c3eb9))
* propose dynamic-import constant folding design ([0dcc165](https://github.com/rajeev-chaurasia/acquit/commit/0dcc1653747bdca0af4e39d6529d05216e30d996))
* propose re-export narrowing design ([bef923d](https://github.com/rajeev-chaurasia/acquit/commit/bef923dd81cea0ad6a56a0e1d54472f3d268ca92))
* public-presentation pass ([74f37e5](https://github.com/rajeev-chaurasia/acquit/commit/74f37e5765ed83247f6dec812f8b518357d429d1))
* publish flask, rich, and httpx replay results ([bd2d902](https://github.com/rajeev-chaurasia/acquit/commit/bd2d902e6d5ca4f49c11526824fde03287c0eab0))
* **study:** narrowing-arm results for flask, rich, httpx, and black ([0b44059](https://github.com/rajeev-chaurasia/acquit/commit/0b44059a95c6fdb89596e4112f039afb035d8fb2))
* **study:** uvicorn and black manifests and frozen environments ([1e634dc](https://github.com/rajeev-chaurasia/acquit/commit/1e634dcc881508212a0a6a1056d40edde61369b5))
* **study:** uvicorn narrowing-arm results and the campaign table ([727cf68](https://github.com/rajeev-chaurasia/acquit/commit/727cf686ce4138562e72afccf4da66b54508f9ee))
* the precision campaign chapter ([be2a432](https://github.com/rajeev-chaurasia/acquit/commit/be2a432cb5161a8facdca9df7b5e24f89236f851))
