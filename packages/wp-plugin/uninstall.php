<?php
/**
 * Desinstalación: borra solo los datos de Faro (opciones faro_connection y faro_pairing, y los
 * transients de nonces y del límite por IP), en cada sitio si es una red multisitio.
 * Desactivar el plugin no borra datos.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;
defined( 'WP_UNINSTALL_PLUGIN' ) || exit;

$faro_uninstall_site = static function (): void {
	global $wpdb;

	delete_option( 'faro_connection' );
	delete_option( 'faro_pairing' );

	// No hay función de WordPress para borrar transients por prefijo.
	// phpcs:ignore WordPress.DB.DirectDatabaseQuery.DirectQuery, WordPress.DB.DirectDatabaseQuery.NoCaching -- Borrado único al desinstalar.
	$wpdb->query(
		$wpdb->prepare(
			"DELETE FROM {$wpdb->options} WHERE option_name LIKE %s OR option_name LIKE %s OR option_name LIKE %s OR option_name LIKE %s",
			$wpdb->esc_like( '_transient_faro_nonce_' ) . '%',
			$wpdb->esc_like( '_transient_timeout_faro_nonce_' ) . '%',
			$wpdb->esc_like( '_transient_faro_pair_ip_' ) . '%',
			$wpdb->esc_like( '_transient_timeout_faro_pair_ip_' ) . '%'
		)
	);

	wp_cache_delete( 'faro_connection', 'options' );
	wp_cache_delete( 'faro_pairing', 'options' );
	wp_cache_delete( 'alloptions', 'options' );
	wp_cache_delete( 'notoptions', 'options' );
};

if ( is_multisite() ) {
	$faro_site_ids = get_sites(
		array(
			'fields' => 'ids',
			'number' => 0,
		)
	);
	foreach ( $faro_site_ids as $faro_site_id ) {
		switch_to_blog( (int) $faro_site_id );
		$faro_uninstall_site();
		restore_current_blog();
	}
} else {
	$faro_uninstall_site();
}
