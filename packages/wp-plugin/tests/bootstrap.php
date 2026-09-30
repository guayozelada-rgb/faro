<?php
/**
 * Bootstrap de PHPUnit (WordPress test suite de wp-env).
 *
 * Variables de entorno:
 * - WP_TESTS_DIR: carpeta del test suite (wp-env: /wordpress-phpunit).
 * - FARO_TEST_HPOS: "1" (por defecto) activa HPOS; "0" lo desactiva. Solo si WooCommerce está instalado.
 *
 * @package Faro
 */

$faro_tests_dir = getenv( 'WP_TESTS_DIR' );
if ( false === $faro_tests_dir || '' === $faro_tests_dir ) {
	$faro_tests_dir = '/wordpress-phpunit';
}

// Activa los ganchos de prueba del plugin (Faro_Clock::freeze, Faro_Pairing::set_environment_type).
define( 'FARO_TESTING', true );
define( 'WP_TESTS_PHPUNIT_POLYFILLS_PATH', dirname( __DIR__ ) . '/vendor/yoast/phpunit-polyfills' );
define( 'FARO_TEST_HPOS', '0' !== getenv( 'FARO_TEST_HPOS' ) );

require_once $faro_tests_dir . '/includes/functions.php';

$faro_woocommerce_file = dirname( __DIR__, 2 ) . '/woocommerce/woocommerce.php';

tests_add_filter(
	'muplugins_loaded',
	static function () use ( $faro_woocommerce_file ): void {
		if ( file_exists( $faro_woocommerce_file ) ) {
			require_once $faro_woocommerce_file;
		}
		require_once dirname( __DIR__ ) . '/faro.php';
	}
);

tests_add_filter(
	'setup_theme',
	static function (): void {
		if ( ! class_exists( 'WC_Install' ) ) {
			return;
		}
		WC_Install::install();
		update_option( 'woocommerce_custom_orders_table_enabled', FARO_TEST_HPOS ? 'yes' : 'no' );
		update_option( 'woocommerce_custom_orders_table_data_sync_enabled', 'no' );
		if ( FARO_TEST_HPOS ) {
			wc_get_container()->get( \Automattic\WooCommerce\Internal\DataStores\Orders\DataSynchronizer::class )->create_database_tables();
		}
		$GLOBALS['wp_roles'] = null; // phpcs:ignore WordPress.WP.GlobalVariablesOverride.Prohibited -- Recarga los roles que crea WooCommerce.
		wp_roles();
	}
);

require_once $faro_tests_dir . '/includes/bootstrap.php';
require_once __DIR__ . '/class-faro-test-case.php';
