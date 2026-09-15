#[cfg(feature = "demo")]
mod demo_backend;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default();
    #[cfg(feature = "wdio")]
    let builder = builder.plugin(tauri_plugin_wdio_webdriver::init());

    #[cfg(feature = "demo")]
    let plugin =
        tauri_plugin_blehub::with_backend(std::sync::Arc::new(demo_backend::DemoBackend::new()));
    #[cfg(not(feature = "demo"))]
    let plugin = tauri_plugin_blehub::init();

    builder
        .plugin(plugin)
        .run(tauri::generate_context!())
        .expect("error while running Tauri internal example");
}
