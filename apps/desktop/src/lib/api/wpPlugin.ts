import { invoke } from "./invoke";

/** Resultado de `wp_plugin_export`: solo el nombre del archivo, nunca la ruta. */
export interface PluginExport {
  file_name: string;
}

/**
 * Guarda `faro-wordpress.zip` en la carpeta Descargas y lo muestra en el explorador
 * (spec F1a §3.3). Errores: `plugin.package_missing`, `plugin.export_failed`.
 */
export function exportWpPlugin(): Promise<PluginExport> {
  return invoke<PluginExport>("wp_plugin_export");
}
