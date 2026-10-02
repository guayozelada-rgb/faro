<?php
/**
 * Escrituras atómicas sobre la tabla de opciones.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Leer, comparar e intercambiar, y borrar condicionalmente una opción directamente en la base.
 *
 * Sirve para cambiar una opción solo si nadie la cambió (o borró) desde que se leyó, sin
 * recrearla nunca: update_option() hace add_option() si la fila ya no existe, y eso revive
 * datos revocados.
 */
final class Faro_Option_Store {

	/**
	 * Valor guardado de la opción tal como está en la base (serializado), o null si no existe.
	 * No usa la caché de objetos.
	 *
	 * @param string $option Nombre de la opción.
	 * @return string|null
	 */
	public static function read_raw( string $option ): ?string {
		global $wpdb;
		// phpcs:ignore WordPress.DB.DirectDatabaseQuery.DirectQuery, WordPress.DB.DirectDatabaseQuery.NoCaching -- Lectura para comparar e intercambiar; no debe venir de la caché.
		$raw = $wpdb->get_var( $wpdb->prepare( "SELECT option_value FROM {$wpdb->options} WHERE option_name = %s", $option ) );

		return is_string( $raw ) ? $raw : null;
	}

	/**
	 * Reemplaza el valor guardado solo si la fila existe y su valor no cambió desde que se leyó.
	 * Nunca crea la opción.
	 *
	 * @param string $option       Nombre de la opción.
	 * @param string $expected_raw Valor leído (serializado, exacto).
	 * @param string $new_raw      Valor nuevo (serializado).
	 * @return bool true si esta petición hizo el cambio.
	 */
	public static function compare_and_swap( string $option, string $expected_raw, string $new_raw ): bool {
		global $wpdb;
		// phpcs:ignore WordPress.DB.DirectDatabaseQuery.DirectQuery, WordPress.DB.DirectDatabaseQuery.NoCaching -- Comparar e intercambiar atómico; la caché se invalida abajo.
		$rows = $wpdb->query(
			$wpdb->prepare(
				"UPDATE {$wpdb->options} SET option_value = %s WHERE option_name = %s AND BINARY option_value = %s",
				$new_raw,
				$option,
				$expected_raw
			)
		);
		self::flush_cache( $option );

		return 1 === $rows;
	}

	/**
	 * Borra la opción solo si su valor no cambió desde que se leyó.
	 *
	 * @param string $option       Nombre de la opción.
	 * @param string $expected_raw Valor leído (serializado, exacto).
	 * @return bool true si esta petición la borró.
	 */
	public static function delete_if_unchanged( string $option, string $expected_raw ): bool {
		global $wpdb;
		// phpcs:ignore WordPress.DB.DirectDatabaseQuery.DirectQuery, WordPress.DB.DirectDatabaseQuery.NoCaching -- Borrado condicional atómico; la caché se invalida abajo.
		$rows = $wpdb->query(
			$wpdb->prepare(
				"DELETE FROM {$wpdb->options} WHERE option_name = %s AND BINARY option_value = %s",
				$option,
				$expected_raw
			)
		);
		self::flush_cache( $option );

		return 1 === $rows;
	}

	/**
	 * Invalida la caché de objetos de la opción.
	 *
	 * @param string $option Nombre de la opción.
	 * @return void
	 */
	public static function flush_cache( string $option ): void {
		wp_cache_delete( $option, 'options' );
		wp_cache_delete( 'notoptions', 'options' );
		wp_cache_delete( 'alloptions', 'options' );
	}
}
