# nvcurve-gui — Güvenlik Modeli ve Değişiklik Kaydı

Bu paket, Centurion'daki NVIDIA Curve Optimizer'ın güvenlik katmanıyla birlikte
ayrıştırılmış halidir. Önceki (Python/PySide6) sürümün sertleştirme maddeleri
(`server.py`, `daemon.py`, token/socket işleri) artık geçerli değil: HTTP sunucusu,
daemon ve `python3 -m nvcurve` alt süreçleri yok. Aşağıdaki her madde kodda
doğrulanmıştır.

---

## 1.0.0 — Centurion'dan ayrıştırma

### Kaldırılan saldırı yüzeyi

- **Yorumlayıcı yok.** Root helper (`nvcurve-root-helper`) Rust ve tek süreçte çalışır;
  `python3`, `PYTHONPATH`, çocuk süreç ortamı sertleştirmesi gerekmez.
- **Ağ yok.** HTTP sunucu / WebSocket / unix socket daemon'u pakete alınmadı.
- **Centurion'a özgü hook'lar çıkarıldı:** `/run/centurion/calibrating.json` kontrolü,
  curve journal (`/var/lib/centurion/oc-journal.jsonl`), `tune-helper` üzerinden
  modprobe (`NVreg_*`) seçenekleri. Pakette artık `/var/lib/centurion` altına yazan
  veya `tune-helper`'ı çağıran kod yok.

### Root helper (`nvcurve-root-helper`)

- Yalnızca root çalıştırabilir (`geteuid() == 0`); `umask 022` sabitlenir (pkexec
  çağıranın umask'ini miras bırakır); `RLIMIT_CORE = 0` (çökme, NvAPI oturum
  belleğini dosyaya bırakmaz).
- **15 işlemlik allowlist** (`dispatch`); bilinmeyen `op` reddedilir.
- stdin ≤ 1 MiB, geçerli UTF-8, JSON nesnesi olmak zorunda.
- Profil adı **kanonik** olmak zorunda (harf/rakam, boşluk, `_ - ( )`, ≤ 64 karakter);
  yol `/etc/nvcurve/profiles/<ad>.json` olarak helper'ın kendisi kurar.
- `profile_data` / `content` için **şema doğrulaması**: bilinmeyen alan reddedilir,
  `bool` tamsayı sayılmaz, her alan için makul aralık, `curve_deltas` anahtarları
  `0..254` tamsayı dizgesi, değerleri sürücü tabanı (−2000 MHz) ile
  `max_delta_khz` (varsayılan +500 MHz, sürücünün +1000 MHz sınırının üstüne
  **çıkamaz**) arasında. Güç limiti ve bellek ofseti ayrıca uygulama anında
  sürücünün canlı aralığına karşı denetlenir (`safety::validate_limits`).
- `max_delta_khz` yalnızca root'a ait `/etc/nvcurve/config.json`'dan okunur: dosya
  root sahipli, grup/diğer yazamaz, normal dosya (symlink değil) ve ≤ 256 KiB değilse
  varsayılanlara dönülür; geçersiz alan tek başına atılır, diğerleri korunur.
- GPU yazmaları `/run/nvcurve.lock` üzerinde `flock` ile serileşir (`O_NOFOLLOW`, 0600,
  en çok 60 sn bekleme): CLI, autoload ve GUI iç içe geçip hiçbir profilin tarif etmediği
  karışık bir durum bırakamaz.
- GUI'ye dönen sonuç dosyaları `/run/nvcurve-gui` altına yazılır: dizin symlink olamaz ve
  root'a ait olmak zorunda; dosya geçici dosya + `fsync` + `rename` ile atomik yazılır
  (önceden yerleştirilmiş bir bağ üzerinden açılmaz).

### polkit

- `com.nvcurve.gui.policy`: iki eylem, her biri `exec.path` ile kendi helper'ına **sabitlenmiş**
  (bu ek açıklama yoksa pkexec genel `org.freedesktop.policykit.exec` eylemine düşüp her
  çağrıda parola sorar). Varsayılan: `set-params` → `auth_admin_keep`; yalnızca salt okunur
  `sensors` → `allow_active=yes`.
- `49-nvcurve-gui.rules`: **açık allowlist** (`ACTION_IDS`); `com.nvcurve.gui.*` ad alanında
  listede olmayan her kimlik `AUTH_ADMIN` alır — önek eşleşmesiyle sessiz izin yok.
  `install.sh`, politikadaki her eylem kimliğinin kurallarda tanımlı olduğunu denetler,
  değilse kurulumu durdurur.
- **Güvenlik seviyesi** (`install.sh --security-level=N`): 1 = `wheel`/`sudo` üyesi, yerel
  + aktif oturum parolasız; 2 = helper her zaman parola ister (polkit kısa süre saklar),
  yalnızca salt okunur sensör akışı sessiz kalır. SSH, başka koltuk, servis hesapları her
  seviyede `AUTH_ADMIN` alır. Yükseltmede mevcut kural dosyasının seviyesi korunur.
  (Centurion'daki seviye 3 — onaylı preset'i adıyla uygulama — bu pakette tek helper ve
  preset deposu olmadığı için yok.)
- `nvcurve-root-helper` `root:root 0700` kurulur: yetkisiz kullanıcı içeriğini okuyamaz
  veya değiştiremez; polkit'in sabitlediği yol bu dosyadır.

### GUI (pkexec istemcisi)

- `pkexec` yalnızca `/usr/bin/pkexec` veya `/bin/pkexec`'ten çalıştırılır — `$PATH` ile
  çözülmez. Helper yolu derleme zamanında sabit (`NVCURVE_HELPER_DIR`).
- Helper çıktısı 4 MiB'ı aşarsa süreç öldürülür; zaman aşımında `pkexec` öldürülür, GUI
  donmaz (işlem asenkron).
- **Combobox üzerinde fare tekerleği değeri değiştirmez** (kasıtlı, kaldırılmamalı —
  `main.cpp`): profil seçimi GPU'ya uygulanan değeri belirlediği için sayfa kaydırırken
  yanlışlıkla değişemez. Tekerlek üst widget'a iletilir.
- Tek örnek: kullanıcıya özel `QLockFile` (`$XDG_RUNTIME_DIR` veya 0700 önbellek dizini;
  paylaşılan `/tmp` asla); iki pencere birbirinin değiştirdiği eğriyi eski halde göstermez.
- **Trial apply**: uygulamadan sonra 30 sn içinde *Keep* basılmazsa eğri stok'a döner.

### Boot guard (`nvcurve-boot-guard`)

Aşırı agresif bir eğri her açılışta uygulanıp makineyi çökerten bir döngüye girebilir.
Guard her açılışta "armed" olur; 180 sn kararlı çalışma veya temiz kapanışta "disarm"
olur. Silahlıyken ölen bir önceki açılış tespit edilirse `nvcurve-autoload` **atlanır**
(`ExecCondition` / OpenRC `start`) ve kullanıcı `nvcurve-boot-guard reset` diyene kadar
atlanmaya devam eder — otomatik geri açılmaz, çökme/atlama/çökme döngüsü olmaz.
Durum `/var/lib/nvcurve/boot-guard.json`: root sahipli dizin, `O_NOFOLLOW` ile açılan ve
tanımlayıcı üzerinde root sahipliği/izin denetlenen dosya, atomik yazım.

### Derleme sertleştirmesi

- GUI: `-fstack-protector-strong`, `-fstack-clash-protection`, `_FORTIFY_SOURCE=3`,
  `_GLIBCXX_ASSERTIONS`, `-ftrivial-auto-var-init=zero`, `-fcf-protection=full`, PIE,
  full RELRO, `noexecstack` (`--no-harden` ile kapatılır).
- Rust: fat LTO, `panic = "abort"`, `strip`; `Cargo.lock` sabit (`cargo build --locked`).
- `install.sh` derlemeyi `SUDO_USER` olarak yapar, yalnızca kurulumu root olarak yapar.
