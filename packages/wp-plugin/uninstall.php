<?php
/**
 * Desinstalación: borra las opciones y los transients faro_* (nada más).
 * Desactivar el plugin no borra datos.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;
defined( 'WP_UNINSTALL_PLUGIN' ) || exit;

delete_option( 'faro_connection' );
delete_option( 'faro_pairing' );

global $wpdb;

// Transients de nonces y del límite por IP, y cualquier otra opción faro_* que quede.
// phpcs:ignore WordPress.DB.DirectDatabaseQuery.DirectQuery, WordPress.DB.DirectDatabaseQuery.NoCaching -- No hay función de WordPress para borrar transients por prefijo.
$wpdb->query(
	$wpdb->prepare(
		"DELETE FROM {$wpdb->options} WHERE option_name LIKE %s OR option_name LIKE %s OR option_name LIKE %s",
		$wpdb->esc_like( 'faro_' ) . '%',
		$wpdb->esc_like( '_transient_faro_' ) . '%',
		$wpdb->esc_like( '_transient_timeout_faro_' ) . '%'
	)
);

wp_cache_delete( 'alloptions', 'options' );
wp_cache_delete( 'notoptions', 'options' );
