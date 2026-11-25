//! nvcurve-sensors — root-only NVIDIA temperatures for the NVIDIA tab.
//!
//! Blackwell's hotspot and the per-partition GDDR7 sensors are GPU register
//! reads that the driver only allows a privileged client. The tab starts this
//! through pkexec once per show (a read-only polkit action, no password for
//! the active local session) and it streams one JSON line every 2 s:
//!   {"hotspot_c": 45, "vram_c": 52, "vram_partitions": [["A0", 50], …],
//!    "gpus": [{"index": 0, "hotspot_c": 45, …}, {"index": 1, …}]}
//! The top-level fields are GPU 0; `gpus` has every GPU by NVAPI index.
//!
//! It takes no arguments and no input: the only thing it can do is read the
//! fixed sensor offsets in hal::sensors. It stops when the tab closes its
//! stdin (EOF), when stdout goes away, when its parent dies, or after an hour.

use nvcurve::hal::{gpu, sensors};
use nvcurve::nvml;
use nvcurve::ops;
use std::io::{Read, Write};
use std::time::{Duration, Instant};

const INTERVAL: Duration = Duration::from_secs(2);
const MAX_RUNTIME: Duration = Duration::from_secs(3600);

fn main() {
    if unsafe { libc::geteuid() } != 0 {
        eprintln!("nvcurve-sensors must run as root (via pkexec)");
        std::process::exit(1);
    }
    unsafe {
        let zero = libc::rlimit { rlim_cur: 0, rlim_max: 0 };
        libc::setrlimit(libc::RLIMIT_CORE, &zero);
        libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
        if libc::getppid() == 1 { std::process::exit(0); }  // parent already gone
    }
    // stdin EOF = the tab was hidden.
    std::thread::spawn(|| {
        let mut b = [0u8; 64];
        while matches!(std::io::stdin().read(&mut b), Ok(n) if n > 0) {}
        std::process::exit(0);
    });
    let gpus = match gpu::init_nvapi().and_then(|_| gpu::enumerate_gpus()) {
        Ok(g) => g,
        Err(_) => {
            println!("{{\"error\":\"no NVIDIA GPU\"}}");
            std::process::exit(1);
        }
    };
    // Architecture through the PCI-bus matched NVML device; unknown (no Blackwell
    // register reads) when the mapping is ambiguous.
    let single = gpus.len() == 1;
    let targets: Vec<_> = gpus.iter().enumerate().map(|(i, &g)| {
        let ni = if single { Some(0) } else { gpu::nvml_index(i).ok() };
        let arch = ni
            .and_then(|ni| nvml::ready().ok().and_then(|n| n.architecture(n.handle(ni).ok()?).ok()));
        (g, arch)
    }).collect();
    let start = Instant::now();
    let mut out = std::io::stdout().lock();
    while start.elapsed() < MAX_RUNTIME {
        // Never sample registers while another nvcurve process is writing an OC table.
        let all: Vec<sensors::Sensors> = match ops::gpu_try_lock() {
            Ok(Some(_lock)) => targets.iter().map(|&(g, arch)| sensors::read(g, arch)).collect(),
            Ok(None) => targets.iter().map(|_| sensors::Sensors::default()).collect(), // GPU write in progress
            Err(e) => {
                eprintln!("sensor read skipped: {e}");
                targets.iter().map(|_| sensors::Sensors::default()).collect()
            }
        };
        let per_gpu: Vec<_> = all.iter().enumerate().map(|(i, s)| serde_json::json!({
            "index": i, "hotspot_c": s.hotspot_c, "vram_c": s.vram_c, "vram_partitions": s.vram_partitions,
        })).collect();
        let s = &all[0];
        let line = serde_json::json!({
            "hotspot_c": s.hotspot_c, "vram_c": s.vram_c, "vram_partitions": s.vram_partitions, "gpus": per_gpu,
        });
        if writeln!(out, "{line}").and_then(|_| out.flush()).is_err() { break; }
        std::thread::sleep(INTERVAL);
    }
}
