<?php
/**
 * Arranque del plugin.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Registra los hooks del plugin. El plugin nunca hace peticiones salientes.
 */
final class Faro_Plugin {

	/**
	 * Registra los hooks.
	 *
	 * @return void
	 */
	public static function init(): void {
		add_action( 'init', array( self::class, 'load_textdomain' ) );
		add_action( 'rest_api_init', array( Faro_Rest::class, 'register_routes' ) );
		add_filter( 'rest_request_before_callbacks', array( Faro_Rest::class, 'before_callbacks' ), 10, 3 );
		add_filter( 'rest_post_dispatch', array( Faro_Rest::class, 'no_store' ), 10, 3 );
		add_action( 'before_woocommerce_init', array( Faro_WooCommerce::class, 'declare_compatibility' ) );

		if ( is_admin() ) {
			Faro_Admin::init();
		}
	}

	/**
	 * Carga las traducciones del dominio faro.
	 *
	 * @return void
	 */
	public static function load_textdomain(): void {
		load_plugin_textdomain( 'faro', false, dirname( plugin_basename( FARO_PLUGIN_FILE ) ) . '/languages' );
	}
}
