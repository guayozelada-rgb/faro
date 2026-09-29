<?php
/**
 * Integración con WooCommerce (solo API CRUD, compatible con HPOS).
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Lee productos con wc_get_products y declara compatibilidad con HPOS.
 * Nunca consulta wp_posts directamente para productos o pedidos.
 */
final class Faro_WooCommerce {

	/**
	 * Declara compatibilidad con las tablas de pedidos de alto rendimiento (HPOS).
	 *
	 * @return void
	 */
	public static function declare_compatibility(): void {
		if ( class_exists( \Automattic\WooCommerce\Utilities\FeaturesUtil::class ) ) {
			\Automattic\WooCommerce\Utilities\FeaturesUtil::declare_compatibility( 'custom_order_tables', FARO_PLUGIN_FILE, true );
		}
	}

	/**
	 * Indica si WooCommerce está activo.
	 *
	 * @return bool
	 */
	public static function is_active(): bool {
		return class_exists( 'WooCommerce' ) && function_exists( 'wc_get_products' );
	}

	/**
	 * Versión de WooCommerce, o null si no está activo.
	 *
	 * @return string|null
	 */
	public static function version(): ?string {
		if ( ! self::is_active() || ! defined( 'WC_VERSION' ) ) {
			return null;
		}

		return (string) constant( 'WC_VERSION' );
	}

	/**
	 * Si HPOS está en uso, o null si WooCommerce no está activo o no se puede saber.
	 *
	 * @return bool|null
	 */
	public static function hpos_enabled(): ?bool {
		if ( ! self::is_active() || ! class_exists( \Automattic\WooCommerce\Utilities\OrderUtil::class ) ) {
			return null;
		}

		return (bool) \Automattic\WooCommerce\Utilities\OrderUtil::custom_orders_table_usage_is_enabled();
	}

	/**
	 * Número de productos publicados, o null si WooCommerce no está activo.
	 *
	 * @return int|null
	 */
	public static function count_products(): ?int {
		if ( ! self::is_active() ) {
			return null;
		}
		$result = wc_get_products(
			array(
				'status'   => 'publish',
				'limit'    => 1,
				'paginate' => true,
				'return'   => 'ids',
			)
		);

		return is_object( $result ) && isset( $result->total ) ? (int) $result->total : 0;
	}

	/**
	 * Página de productos publicados.
	 *
	 * @param int $page     Página (desde 1).
	 * @param int $per_page Elementos por página.
	 * @return array{items: list<array{id: int, title: string, url: string, slug: string, modified_at: string}>, total: int}
	 */
	public static function list_products( int $page, int $per_page ): array {
		if ( ! self::is_active() ) {
			return array(
				'items' => array(),
				'total' => 0,
			);
		}
		$result = wc_get_products(
			array(
				'status'   => 'publish',
				'limit'    => $per_page,
				'page'     => $page,
				'paginate' => true,
				'orderby'  => 'ID',
				'order'    => 'ASC',
			)
		);
		$items  = array();
		$total  = 0;
		if ( is_object( $result ) && isset( $result->products, $result->total ) && is_array( $result->products ) ) {
			$total = (int) $result->total;
			foreach ( $result->products as $product ) {
				if ( ! $product instanceof WC_Product ) {
					continue;
				}
				$modified = $product->get_date_modified() ?? $product->get_date_created();
				$items[]  = array(
					'id'          => $product->get_id(),
					'title'       => Faro_Content::plain_text( $product->get_name() ),
					'url'         => (string) $product->get_permalink(),
					'slug'        => $product->get_slug(),
					'modified_at' => Faro_Clock::iso( null !== $modified ? $modified->getTimestamp() : 0 ),
				);
			}
		}

		return array(
			'items' => $items,
			'total' => $total,
		);
	}
}
