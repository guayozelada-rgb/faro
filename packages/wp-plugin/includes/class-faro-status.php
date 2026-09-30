<?php
/**
 * Estado del sitio para la app.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Versiones, conteos, WooCommerce, HPOS, plugin SEO y datos públicos de la conexión.
 */
final class Faro_Status {

	/**
	 * Datos del sitio.
	 *
	 * @return array{name: string, home_url: string, wp_version: string, plugin_version: string}
	 */
	public static function site(): array {
		return array(
			'name'           => Faro_Content::plain_text( (string) get_bloginfo( 'name' ) ),
			'home_url'       => untrailingslashit( home_url() ),
			'wp_version'     => (string) get_bloginfo( 'version' ),
			'plugin_version' => FARO_VERSION,
		);
	}

	/**
	 * Estado completo (respuesta de GET /faro/v1/status).
	 *
	 * @return array<string, mixed>
	 */
	public static function get(): array {
		$site       = self::site();
		$connection = Faro_Connection::get();
		$pages      = wp_count_posts( 'page' );
		$posts      = wp_count_posts( 'post' );

		return array(
			'api_version'    => FARO_API_VERSION,
			'plugin_version' => FARO_VERSION,
			'wp_version'     => $site['wp_version'],
			'site_name'      => $site['name'],
			'home_url'       => $site['home_url'],
			'woocommerce'    => array(
				'active'       => Faro_WooCommerce::is_active(),
				'version'      => Faro_WooCommerce::version(),
				'hpos_enabled' => Faro_WooCommerce::hpos_enabled(),
			),
			'seo_plugin'     => Faro_Seo_Detector::detect(),
			'counts'         => array(
				'pages'    => (int) ( $pages->publish ?? 0 ),
				'posts'    => (int) ( $posts->publish ?? 0 ),
				'products' => Faro_WooCommerce::count_products(),
			),
			'connection'     => null === $connection ? null : array(
				'connection_id' => $connection['connection_id'],
				'created_at'    => Faro_Clock::iso( $connection['created_at'] ),
			),
		);
	}
}
