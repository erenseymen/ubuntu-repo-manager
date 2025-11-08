## Product Features Backlog

- [ ] Implement official sources control toggles for `main`, `restricted`, `universe`, `multiverse`, and channel variants (security, updates, backports, proposed) without editing `/etc/apt/sources.list`.
- [ ] Build mirror benchmarking workflow that fetches Ubuntu mirror inventory, collects health metrics, and enables one-click switching.
- [ ] Implement PPA management suite: validated addition of Launchpad PPAs and clean removal with keyring cleanup.
- [ ] Support third-party repository management (append, disable, purge entries) via dedicated `sources.list.d` files.
- [ ] Provide authentication key management UI to browse keyring, import fingerprints from Ubuntu keyserver, and remove stale keys.
- [ ] Add foreign package audit view to list non-Ubuntu/Canonical packages and enable selective purge.
- [ ] Deliver maintenance dashboard with guided fixes for core `apt` and `dpkg` recovery commands.
- [ ] Annotate common `apt` failure logs with remediation hints (missing keys, dpkg lock, hash mismatch, etc.).
- [ ] Ensure privilege escalation flows through `pkexec` with `sudo` fallback for protected operations.
- [ ] Integrate background task runner to keep GTK UI responsive during maintenance workflows.
- [ ] Verify all features of https://github.com/linuxmint/mintsources are implemented.
- [ ] Create Flatpak package for Gnome 48 and publish the app on Flathub.
- [x] UI requirement0: Program arayüzü sekmeli bir görünümde olacak, fakat sekmeler solda liste olarak gözükmeli. Sekmeler arasında Ctrl+TAB ile gezinilebilir olmalı.
- [x] UI requirement1: 1. sekmede: "sudo apt update" ve "sudo apt full-upgrade" komutlarını başlatan yan yana iki buton olsun. Butonlara basıldığında, pencereye entegre terminalde çıktıyı göstersin.
- [ ] UI requirement1a: Komut çıktısında herhangi bir sorun varsa, program fix'lemeyi önersin.
Komut çıktısında herhangi bir sorun varsa, program fix'lemeyi önersin.
- [ ] UI requirement2: 2. sekmede: Sistemde yüklü olan tüm repolar (PPA'lar dahil) liste halinde gösterilsin. Official repos, PPAs ve diğer repolar ayrı gruplar olarak gösterilmeli. Repolar hakkında yapılabilecek tüm özellikler bu sekmede olmalı. Örneğin, en hızlı server'ı seçme özelliği de burada olsun.
- [ ] UI requirement3: 3. sekmede: Authentication key management burada olacak.
- [ ] UI requirement4: 4. sekmede: Foreign package management burada olacak.
- [ ] UI requirement5: 5. sekmede: Maintenance/Fix ekranı burada olacak. Koşulabilecek farklı komutlar buton halinde bu ekranda olmalı. Butonların üzerine mouse ile gelince ne yaptığını açıklayan bir help popup'ı gözüksün. Butona basıldığında pencereye entegre terminalde komutu koşsun. Komutların çıktısı bir fix gerektiriyorsa kullanıcıya sorulsun.