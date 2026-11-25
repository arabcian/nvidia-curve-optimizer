//! GPU discovery and initialization (port of hal/gpu.py).

use crate::nvapi::{call0, call2, fid, Gpu, NvError};
use crate::nvml;
use crate::types::GpuInfo;
use std::ffi::{c_void, CStr};

/// NvAPI's documented ceiling for EnumPhysicalGPUs.
const MAX_PHYSICAL_GPUS: usize = 64;

pub fn init_nvapi() -> Result<(), NvError> {
    match call0(fid::INITIALIZE)? {
        Some(0) => Ok(()),
        _ => Err(NvError::Unavailable("NvAPI_Initialize failed".into())),
    }
}

pub fn enumerate_gpus() -> Result<Vec<Gpu>, NvError> {
    let mut handles = [std::ptr::null_mut::<c_void>(); MAX_PHYSICAL_GPUS];
    let mut n: i32 = 0;
    let rc = call2(fid::ENUM_PHYSICAL_GPUS,
                   handles.as_mut_ptr() as *mut c_void,
                   &mut n as *mut i32 as *mut c_void)?
        .ok_or_else(|| NvError::Unavailable("EnumPhysicalGPUs not exported by driver".into()))?;
    if rc != 0 {
        return Err(NvError::Unavailable(format!("EnumPhysicalGPUs failed (code {rc})")));
    }
    if n <= 0 {
        return Err(NvError::NoGpu);
    }
    // Never trust a count larger than the array we handed the driver.
    let count = n as usize;
    if count > MAX_PHYSICAL_GPUS || handles[..count].iter().any(|p| p.is_null())
        || handles[..count].iter().collect::<std::collections::HashSet<_>>().len() != count {
        return Err(NvError::Unavailable("Invalid or duplicate physical GPU handles".into()));
    }
    Ok(handles[..count].iter().map(|p| Gpu(*p as usize)).collect())
}

pub fn gpu_name(gpu: Gpu) -> String {
    let mut buf = [0u8; 256];
    let _ = call2(fid::GET_FULL_NAME, gpu.ptr(), buf.as_mut_ptr() as *mut c_void);
    *buf.last_mut().unwrap() = 0;
    CStr::from_bytes_until_nul(&buf).map(|c| c.to_string_lossy().into_owned()).unwrap_or_default()
}

/// Initialize NvAPI and return (handle, name) for `index`.
pub fn get_gpu(index: usize) -> Result<(Gpu, String), NvError> {
    init_nvapi()?;
    let gpus = enumerate_gpus()?;
    let gpu = *gpus.get(index).ok_or_else(|| NvError::GpuIndex(
        format!("GPU index {index} out of range (found {} GPU(s))", gpus.len())))?;
    Ok((gpu, gpu_name(gpu)))
}

/// Identity of the GPU at NVAPI `index`, bound to its NVML device.
///
/// NVAPI and NVML enumeration order is NOT a join key. One visible GPU in
/// both APIs is matched directly; with several GPUs each NVAPI handle is
/// matched to the NVML device on the same PCI bus (LACT's
/// `find_matching_gpu(bus_id)`), and only a complete, unique bijection with
/// equal model names is accepted. Anything ambiguous fails closed.
pub fn curve_device_identity(index: usize, gpu: Gpu, name: &str) -> Result<serde_json::Value, String> {
    let handles = enumerate_gpus().map_err(|e| e.to_string())?;
    if handles.get(index) != Some(&gpu) {
        return Err("Cannot establish unambiguous NVAPI/NVML GPU identity".into());
    }
    if handles.len() == 1 { return single_gpu_identity(gpu, name); }
    let rows = inventory_mapping(&handles)?;
    let row = &rows[index];
    let n = nvml::ready()?;
    let h = n.fresh_handle(row.nvml_index)?;
    if !same_model(name, &n.device_name(h)?) { return Err("NVAPI and NVML GPU names differ".into()); }
    if n.uuid(h)? != row.uuid || n.pci_identity(h)? != row.pci { return Err("GPU inventory changed".into()); }
    let driver = n.driver_version()?;
    let vbios = n.vbios_version(h)?;
    if driver.is_empty() || vbios.is_empty() { return Err("Missing driver or VBIOS identity".into()); }
    if enumerate_gpus().map_err(|e| e.to_string())? != handles || n.fresh_handle(row.nvml_index)? != h {
        return Err("GPU topology changed during identity capture".into());
    }
    Ok(serde_json::json!({"schema":1,"mapping":"unique_pci_bus","uuid":row.uuid,"pci":row.pci,"driver":driver,"vbios":vbios}))
}

/// NVML device index of the GPU at NVAPI `index`. Every NVML getter/setter
/// for a curve target must use this, never the NVAPI index itself.
pub fn nvml_index(index: usize) -> Result<u32, String> {
    let handles = enumerate_gpus().map_err(|e| e.to_string())?;
    if index >= handles.len() {
        return Err(format!("GPU index {index} out of range (found {} GPU(s))", handles.len()));
    }
    if handles.len() == 1 {
        single_gpu_identity(handles[0], &gpu_name(handles[0]))?;
        return Ok(0);
    }
    Ok(inventory_mapping(&handles)?[index].nvml_index)
}

fn same_model(a: &str, b: &str) -> bool {
    fn normalized(s: &str) -> &str { s.trim().strip_prefix("NVIDIA ").unwrap_or(s.trim()) }
    !a.is_empty() && normalized(a) == normalized(b)
}

/// One visible physical GPU in both APIs, equal model names, a fresh NVML handle.
fn single_gpu_identity(gpu: Gpu, name: &str) -> Result<serde_json::Value, String> {
    let n = nvml::ready()?;
    if enumerate_gpus().map_err(|e| e.to_string())?.as_slice() != [gpu] || n.device_count()? != 1 {
        return Err("Cannot establish unambiguous NVAPI/NVML GPU identity".into());
    }
    let h = n.fresh_handle(0)?;
    if n.handle(0)? != h { return Err("GPU handle changed; restart the helper".into()); }
    let nvml_name = n.device_name(h)?;
    if !same_model(name, &nvml_name) {
        return Err("NVAPI and NVML GPU names differ".into());
    }
    let uuid = n.uuid(h)?;
    if !valid_gpu_uuid(&uuid) { return Err("Invalid GPU UUID".into()); }
    let driver = n.driver_version()?;
    let vbios = n.vbios_version(h)?;
    if driver.is_empty() || vbios.is_empty() { return Err("Missing driver or VBIOS identity".into()); }
    let pci = n.pci_identity(h)?;
    if n.device_count()? != 1 || n.fresh_handle(0)? != h || enumerate_gpus().map_err(|e| e.to_string())?.as_slice() != [gpu] {
        return Err("GPU topology changed during identity capture".into());
    }
    Ok(serde_json::json!({"schema":1,"mapping":"single_visible_physical_gpu","uuid":uuid,"pci":pci,"driver":driver,"vbios":vbios}))
}
fn valid_gpu_uuid(s: &str) -> bool {
    let Some(s) = s.strip_prefix("GPU-") else { return false; };
    s.len() == 36 && s.bytes().enumerate().all(|(i,b)| if [8,13,18,23].contains(&i) { b == b'-' } else { b.is_ascii_hexdigit() })
}

// Bus-only evidence is sufficient ONLY when it forms a complete, unique
// bijection across both APIs (LACT matches NVML → NVAPI by bus number the
// same way). Duplicate bus numbers across PCI domains/slots must remain
// ambiguous. Writes additionally require equal model names and unique,
// re-read UUID/PCI identities (curve_device_identity).
fn bus_mapping(api: &[u32], ml: &[u32]) -> Option<Vec<usize>> {
    if api.is_empty() || api.len() != ml.len() || api.len() > MAX_PHYSICAL_GPUS { return None; }
    let mut used = std::collections::HashSet::new();
    let mut result = Vec::new();
    for bus in api {
        if *bus > 255 { return None; }
        let mut matches = ml.iter().enumerate().filter(|(_, b)| *b == bus);
        let (index, _) = matches.next()?;
        if matches.next().is_some() || !used.insert(index) { return None; }
        result.push(index);
    }
    Some(result)
}
fn api_bus(gpu: Gpu) -> Result<u32, String> {
    let mut bus = u32::MAX;
    let rc = call2(fid::GET_BUS_ID, gpu.ptr(), &mut bus as *mut u32 as *mut c_void).map_err(|e| e.to_string())?;
    if rc != Some(0) || bus > 255 { return Err("NVAPI PCI bus unavailable".into()); }
    Ok(bus)
}
#[derive(PartialEq, Eq)]
struct InventoryRow { handle: nvml::Handle, bus: u32, uuid: String, pci: String, name: String }
struct InventoryIdentity { uuid: String, bus: u32, pci: String, nvml_index: u32 }
/// Fresh read-only join; no index-order assumption, no ambiguous partial join.
fn inventory_mapping(handles: &[Gpu]) -> Result<Vec<InventoryIdentity>, String> {
    let n = nvml::ready()?;
    let count = n.device_count()? as usize;
    if count != handles.len() || count == 0 || count > MAX_PHYSICAL_GPUS { return Err("GPU enumeration count differs".into()); }
    if count == 1 {
        let name = gpu_name(handles[0]);
        let d = single_gpu_identity(handles[0], &name)?;
        let h = n.fresh_handle(0)?;
        let bus = n.pci_bus(h)?;
        if n.uuid(h)? != d["uuid"].as_str().unwrap_or("") || n.pci_identity(h)? != d["pci"].as_str().unwrap_or("") {
            return Err("GPU inventory changed".into());
        }
        if n.device_count()? != 1 || n.fresh_handle(0)? != h || enumerate_gpus().map_err(|e| e.to_string())?.as_slice() != handles {
            return Err("GPU inventory changed".into());
        }
        return Ok(vec![InventoryIdentity { uuid: d["uuid"].as_str().unwrap_or("").to_owned(), bus, pci: d["pci"].as_str().unwrap_or("").to_owned(), nvml_index: 0 }]);
    }
    let capture = || -> Result<Vec<InventoryRow>, String> {
        (0..count).map(|i| {
            let h = n.fresh_handle(i as u32)?;
            let uuid = n.uuid(h)?;
            if !valid_gpu_uuid(&uuid) { return Err("Invalid GPU UUID".into()); }
            Ok(InventoryRow { handle: h, bus: n.pci_bus(h)?, uuid, pci: n.pci_identity(h)?, name: n.device_name(h)? })
        }).collect()
    };
    let api = handles.iter().map(|&h| api_bus(h)).collect::<Result<Vec<_>, _>>()?;
    let ml = capture()?;
    let buses: Vec<_> = ml.iter().map(|v| v.bus).collect();
    let indices = bus_mapping(&api, &buses).ok_or("Ambiguous NVAPI/NVML PCI buses")?;
    let mut uuids = std::collections::HashSet::new();
    let mut pci_ids = std::collections::HashSet::new();
    let mut out = Vec::new();
    for (&g, i) in handles.iter().zip(indices) {
        let row = &ml[i];
        if !same_model(&gpu_name(g), &row.name)
            || !uuids.insert(row.uuid.to_ascii_lowercase()) || !pci_ids.insert(row.pci.clone()) {
            return Err("Ambiguous GPU inventory identity".into());
        }
        out.push(InventoryIdentity { uuid: row.uuid.clone(), bus: row.bus, pci: row.pci.clone(), nvml_index: i as u32 });
    }
    if n.device_count()? as usize != count || enumerate_gpus().map_err(|e| e.to_string())?.as_slice() != handles
        || handles.iter().map(|&h| api_bus(h)).collect::<Result<Vec<_>, _>>()? != api || capture()? != ml {
        return Err("GPU topology changed during inventory".into());
    }
    Ok(out)
}

/// Verified profile target: the GPU at NVAPI `index` with a fresh, unique
/// NVAPI/NVML identity. Returns the NVML index every NVML setter must use.
pub fn require_profile_target(index: usize) -> Result<u32, String> {
    let handles = enumerate_gpus().map_err(|e| e.to_string())?;
    let gpu = *handles.get(index).ok_or_else(|| format!("GPU index {index} out of range (found {} GPU(s))", handles.len()))?;
    curve_device_identity(index, gpu, &gpu_name(gpu))?;
    nvml_index(index)
}

/// All physical GPUs. UUID/PCI/NVML metadata is absent when cross-API inventory
/// cannot be joined safely. The public index remains the NVAPI index.
pub fn discover_gpus() -> Result<Vec<GpuInfo>, NvError> {
    init_nvapi()?;
    let gpus = match enumerate_gpus() {
        Ok(g) => g,
        Err(NvError::NoGpu) => return Ok(Vec::new()),
        Err(e) => return Err(e),
    };
    let mapped = inventory_mapping(&gpus).ok();
    Ok(gpus.iter().enumerate().map(|(i, &g)| {
        let identity = mapped.as_ref().and_then(|m| m.get(i));
        GpuInfo { name: gpu_name(g), index: i, uuid: identity.map(|v| v.uuid.clone()), pci_bus_id: identity.map(|v| v.bus),
            pci_address: identity.map(|v| v.pci.clone()),
            identity_evidence: identity.map(|_| if gpus.len() == 1 { "single_visible_physical_gpu" } else { "unique_pci_bus" }),
            nvml_index: identity.map(|v| v.nvml_index) }
    }).collect())
}

#[cfg(test)]
mod tests {
    #[test]
    fn bus_bijection_rejects_ambiguous_or_missing_devices() {
        use super::bus_mapping;
        assert_eq!(bus_mapping(&[1, 9], &[9, 1]), Some(vec![1, 0]));
        assert_eq!(bus_mapping(&[1, 1], &[1, 9]), None);
        assert_eq!(bus_mapping(&[1, 9], &[1, 1]), None);
        assert_eq!(bus_mapping(&[1, 9], &[1, 8]), None);
        assert_eq!(bus_mapping(&[1], &[1, 9]), None);
        assert_eq!(bus_mapping(&[256], &[256]), None);
        assert_eq!(bus_mapping(&[], &[]), None);
    }
    #[test]
    fn gpu_uuid_format() {
        assert!(super::valid_gpu_uuid("GPU-01234567-89ab-cdef-0123-456789abcdef"));
        for s in ["GPU-broken", "MIG-01234567-89ab-cdef-0123-456789abcdef", "GPU-01234567-89ab-cdef-0123-456789abcdeg"] {
            assert!(!super::valid_gpu_uuid(s));
        }
    }
    #[test]
    fn missing_driver_is_an_error_not_a_crash() {
        if std::path::Path::new("/proc/driver/nvidia").exists() { return; }
        assert!(matches!(super::discover_gpus(), Err(crate::NvError::Unavailable(_))));
        assert!(crate::hal::limits::get_power_limit(0).power_limit_w.is_none());
        assert!(crate::hal::limits::set_power_limit(100, 0).is_err());
    }
}
