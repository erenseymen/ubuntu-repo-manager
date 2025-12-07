## Product Features Backlog

### 🖥️ Tab 1: System Updates
- [x] "apt update" ve "apt full-upgrade" komutlarını başlatan yan yana iki buton. Butonlara basıldığında, pencereye entegre terminalde çıktıyı göstersin.
- [ ] Komut çıktısında herhangi bir sorun varsa, program fix'lemeyi önersin.
- [ ] Annotate common `apt` failure logs with remediation hints (missing keys, dpkg lock, hash mismatch, etc.).

### 📦 Tab 2: Repositories
- [x] Sistemde yüklü olan tüm repolar (PPA'lar dahil) liste halinde gösterilsin. Official repos, PPAs ve diğer repolar ayrı gruplar olarak gösterilmeli.
- [ ] Implement official sources control toggles for `main`, `restricted`, `universe`, `multiverse`, and channel variants (security, updates, backports, proposed) without editing `/etc/apt/sources.list`.
- [ ] Implement PPA management suite: validated addition of Launchpad PPAs and clean removal with keyring cleanup.
- [ ] Support third-party repository management (append, disable, purge entries) via dedicated `sources.list.d` files.

### 🌐 Tab 3: Mirror Selection
- [x] En hızlı mirror'ı bulma ve seçme özelliği. İki aşamalı benchmark (ping + download), sonuçları cache'leme, seçim değiştiğinde ping ile doğrulama.
- [x] Build mirror benchmarking workflow that fetches Ubuntu mirror inventory, collects health metrics, and enables one-click switching.
- [ ] mirror listesini daha efektik nasıl alırım?

### 🔑 Tab 4: Authentication Keys
- [ ] Authentication key management UI: browse keyring, import fingerprints from Ubuntu keyserver, and remove stale keys.

### 📋 Tab 5: Foreign Packages
- [ ] Foreign package audit view to list non-Ubuntu/Canonical packages and enable selective purge.

### 🔧 Tab 6: Maintenance/Fix
- [ ] Maintenance ekranı: Koşulabilecek farklı komutlar buton halinde. Butonların üzerine mouse ile gelince ne yaptığını açıklayan help popup'ı. Butona basıldığında pencereye entegre terminalde komutu koşsun. Komutların çıktısı bir fix gerektiriyorsa kullanıcıya sorulsun.
- [ ] Deliver maintenance dashboard with guided fixes for core `apt` and `dpkg` recovery commands.

---

### ⚙️ General / Cross-Tab Features
- [x] Sekmeli arayüz: Sekmeler solda liste olarak gözükmeli. Sekmeler arasında Ctrl+TAB ile gezinilebilir olmalı.
- [ ] Ensure privilege escalation flows through `pkexec` with `sudo` fallback for protected operations.
- [ ] Integrate background task runner to keep GTK UI responsive during maintenance workflows.

---

### 🚀 Deployment & Publishing
- [ ] Verify all features of https://github.com/linuxmint/mintsources are implemented.
- [ ] Create Flatpak package for Gnome 48 and publish the app on Flathub.