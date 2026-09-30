<?php
/**
 * Pruebas de WooCommerce (con y sin WooCommerce, HPOS activado y desactivado).
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * GET /faro/v1/products, estado de WooCommerce y HPOS.
 */
class Test_Faro_WooCommerce extends Faro_Test_Case {

	/**
	 * Salta la prueba si WooCommerce no está instalado en este entorno.
	 *
	 * @return void
	 */
	private function require_woocommerce(): void {
		if ( ! Faro_WooCommerce::is_active() ) {
			$this->markTestSkipped( 'WooCommerce no está instalado en este entorno (combinación mínima).' );
		}
	}

	/**
	 * Crea un producto simple.
	 *
	 * @param string $name   Nombre.
	 * @param string $status Estado.
	 * @return int
	 */
	private function create_product( string $name, string $status = 'publish' ): int {
		$product = new WC_Product_Simple();
		$product->set_name( $name );
		$product->set_status( $status );
		$product->set_regular_price( '10' );
		return $product->save();
	}

	public function test_hpos_compatibility_is_declared(): void {
		$this->require_woocommerce();
		$controller = wc_get_container()->get( \Automattic\WooCommerce\Internal\Features\FeaturesController::class );

		$compatible = $controller->get_compatible_plugins_for_feature( 'custom_order_tables' );

		$this->assertContains( plugin_basename( FARO_PLUGIN_FILE ), $compatible['compatible'] );
	}

	public function test_hpos_state_matches_environment(): void {
		$this->require_woocommerce();
		$credentials = $this->pair();

		$data = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status' ) )->get_data();

		$this->assertTrue( $data['woocommerce']['active'] );
		$this->assertSame( WC_VERSION, $data['woocommerce']['version'] );
		$this->assertSame( FARO_TEST_HPOS, $data['woocommerce']['hpos_enabled'] );
		$this->assertSame( FARO_TEST_HPOS, \Automattic\WooCommerce\Utilities\OrderUtil::custom_orders_table_usage_is_enabled() );
	}

	public function test_products_published_only_with_plain_titles_and_utc_dates(): void {
		$this->require_woocommerce();
		$credentials = $this->pair();
		$first       = $this->create_product( 'Taza <em>roja</em> &amp; azul' );
		$second      = $this->create_product( 'Plato' );
		$this->create_product( 'Borrador', 'draft' );
		$this->create_product( 'Privado', 'private' );

		$response = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/products', array( 'per_page' => '1' ) ) );
		$this->assertSame( 200, $response->get_status() );
		$data = $response->get_data();

		$this->assertTrue( $data['woocommerce_active'] );
		$this->assertSame( 2, $data['total'] );
		$this->assertSame( 2, $data['total_pages'] );
		$this->assertCount( 1, $data['items'] );
		$item    = $data['items'][0];
		$product = wc_get_product( $first );
		$this->assertInstanceOf( WC_Product::class, $product );
		$this->assertSame( $first, $item['id'] );
		$this->assertSame( 'Taza roja & azul', $item['title'] );
		$this->assertSame( $product->get_permalink(), $item['url'] );
		$this->assertSame( $product->get_slug(), $item['slug'] );
		$this->assertMatchesRegularExpression( '/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/', $item['modified_at'] );
		$this->assertSame( gmdate( 'Y-m-d\TH:i:s\Z', $product->get_date_modified()->getTimestamp() ), $item['modified_at'] );

		$page_two = $this->dispatch(
			$this->signed_request(
				$credentials,
				'GET',
				'/faro/v1/products',
				array(
					'page'     => '2',
					'per_page' => '1',
				)
			)
		)->get_data();
		$this->assertSame( $second, $page_two['items'][0]['id'] );

		$status = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status' ) )->get_data();
		$this->assertSame( 2, $status['counts']['products'] );
	}

	public function test_orders_are_not_exposed(): void {
		$this->require_woocommerce();
		$routes = array_keys( rest_get_server()->get_routes( 'faro/v1' ) );

		foreach ( $routes as $route ) {
			$this->assertStringNotContainsString( 'order', $route );
		}
	}

	public function test_without_woocommerce_products_are_empty(): void {
		if ( Faro_WooCommerce::is_active() ) {
			$this->markTestSkipped( 'WooCommerce está activo en este entorno; esta prueba corre en la combinación mínima.' );
		}
		$credentials = $this->pair();

		$products = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/products' ) );
		$this->assertSame( 200, $products->get_status() );
		$data = $products->get_data();
		$this->assertSame( array(), $data['items'] );
		$this->assertFalse( $data['woocommerce_active'] );
		$this->assertSame( 0, $data['total'] );

		$status = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status' ) )->get_data();
		$this->assertSame(
			array(
				'active'       => false,
				'version'      => null,
				'hpos_enabled' => null,
			),
			$status['woocommerce']
		);
		$this->assertNull( $status['counts']['products'] );
	}
}
