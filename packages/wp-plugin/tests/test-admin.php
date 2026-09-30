<?php
/**
 * Pruebas de la pantalla Ajustes → Faro.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Faro_Admin: generar código, desconectar, permisos, nonces y vista.
 */
class Test_Faro_Admin extends Faro_Test_Case {

	/**
	 * Prepara una petición POST del formulario.
	 *
	 * @param string      $action Acción.
	 * @param string|null $nonce  Nonce o null para no enviarlo.
	 * @return void
	 */
	private function post( string $action, ?string $nonce ): void {
		$_SERVER['REQUEST_METHOD'] = 'POST';
		$_POST                     = array( 'faro_action' => $action );
		if ( null !== $nonce ) {
			$_POST['_wpnonce'] = $nonce;
		}
		$_REQUEST = $_POST;
	}

	/**
	 * Crea y activa un administrador.
	 *
	 * @return void
	 */
	private function login_admin(): void {
		wp_set_current_user( self::factory()->user->create( array( 'role' => 'administrator' ) ) );
	}

	/**
	 * Salida de la vista.
	 *
	 * @return string
	 */
	private function render(): string {
		ob_start();
		try {
			Faro_Admin::render();
		} finally {
			$html = (string) ob_get_clean();
		}
		return $html;
	}

	public function tear_down(): void {
		$_SERVER['REQUEST_METHOD'] = 'GET';
		parent::tear_down();
	}

	public function test_generate_code_with_nonce_and_capability(): void {
		$this->login_admin();
		$this->post( 'generate_code', wp_create_nonce( 'faro_generate_code' ) );

		Faro_Admin::handle_post();

		$code = Faro_Admin::generated_code();
		$this->assertIsString( $code );
		$this->assertMatchesRegularExpression( '/^[0-9]{6}$/', $code );
		$this->assertNotNull( Faro_Pairing::get_pending() );
		$html = $this->render();
		$this->assertStringContainsString( substr( $code, 0, 3 ) . ' ' . substr( $code, 3 ), $html );
		$this->assertStringContainsString( 'Caduca en 10 minutos y solo sirve una vez.', $html );
	}

	public function test_generate_code_without_nonce_is_rejected(): void {
		$this->login_admin();
		$this->post( 'generate_code', null );

		$this->expectException( WPDieException::class );
		try {
			Faro_Admin::handle_post();
		} finally {
			$this->assertNull( Faro_Pairing::get_pending() );
		}
	}

	public function test_generate_code_without_capability_is_rejected(): void {
		wp_set_current_user( self::factory()->user->create( array( 'role' => 'editor' ) ) );
		$this->post( 'generate_code', wp_create_nonce( 'faro_generate_code' ) );

		$this->expectException( WPDieException::class );
		try {
			Faro_Admin::handle_post();
		} finally {
			$this->assertNull( Faro_Pairing::get_pending() );
		}
	}

	public function test_generate_code_on_insecure_site_is_blocked(): void {
		$this->login_admin();
		$this->force_scheme( 'http' );
		Faro_Pairing::set_environment_type( 'production' );
		$this->post( 'generate_code', wp_create_nonce( 'faro_generate_code' ) );

		Faro_Admin::handle_post();

		$this->assertNull( Faro_Admin::generated_code() );
		$this->assertNull( Faro_Pairing::get_pending() );
		$html = $this->render();
		$this->assertStringContainsString( 'Tu sitio no usa HTTPS. Faro solo se conecta a sitios con HTTPS.', $html );
		$this->assertMatchesRegularExpression( '/<input[^>]*disabled/', $html );
	}

	public function test_disconnect_with_nonce_revokes(): void {
		$credentials = $this->pair();
		$this->login_admin();
		$this->post( 'disconnect', wp_create_nonce( 'faro_disconnect' ) );

		Faro_Admin::handle_post();

		$this->assertNull( Faro_Connection::get() );
		$this->assertSame( 'disconnected', Faro_Admin::notice() );
		$after = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status' ) );
		$this->assertSame( 'wp.revoked', $this->error_code( $after ) );
	}

	public function test_disconnect_without_nonce_is_rejected(): void {
		$this->pair();
		$this->login_admin();
		$this->post( 'disconnect', 'nonce-falso' );

		$this->expectException( WPDieException::class );
		try {
			Faro_Admin::handle_post();
		} finally {
			$this->assertNotNull( Faro_Connection::get() );
		}
	}

	public function test_disconnect_without_capability_is_rejected(): void {
		$this->pair();
		wp_set_current_user( self::factory()->user->create( array( 'role' => 'editor' ) ) );
		$this->post( 'disconnect', wp_create_nonce( 'faro_disconnect' ) );

		$this->expectException( WPDieException::class );
		try {
			Faro_Admin::handle_post();
		} finally {
			$this->assertNotNull( Faro_Connection::get() );
		}
	}

	public function test_render_without_capability_is_rejected(): void {
		wp_set_current_user( self::factory()->user->create( array( 'role' => 'editor' ) ) );

		$this->expectException( WPDieException::class );
		$this->render();
	}

	public function test_render_states(): void {
		$this->login_admin();

		$empty = $this->render();
		$this->assertStringContainsString( 'Conecta tu sitio con Faro', $empty );
		$this->assertStringContainsString( 'Generar código de conexión', $empty );
		$this->assertStringContainsString( 'Abre Faro en tu computadora y ve a Configuración → Sitios conectados.', $empty );
		$this->assertStringContainsString( 'name="_wpnonce"', $empty );

		$this->pair();
		$connected = $this->render();
		$this->assertStringContainsString( 'Tu sitio está conectado con Faro desde el', $connected );
		$this->assertStringContainsString( 'Última lectura de Faro:', $connected );
		$this->assertStringContainsString( 'Desconectar de Faro', $connected );

		add_filter(
			'salt',
			static function ( $salt, $scheme ) {
				return 'auth' === $scheme ? 'salts-nuevas-de-prueba' : $salt;
			},
			10,
			2
		);
		$broken = $this->render();
		$this->assertStringContainsString( 'cambiaron las claves de seguridad de WordPress', $broken );
	}

	public function test_code_view_warns_that_existing_connection_will_be_replaced(): void {
		$this->pair();
		$this->login_admin();
		$this->post( 'generate_code', wp_create_nonce( 'faro_generate_code' ) );

		Faro_Admin::handle_post();

		$this->assertStringContainsString( 'Si usas este código, la conexión actual se reemplazará.', $this->render() );
	}

	public function test_settings_link_in_plugin_list(): void {
		$links = Faro_Admin::action_links( array( 'deactivate' => '<a href="#">Desactivar</a>' ) );

		$this->assertStringContainsString( 'options-general.php?page=faro', (string) reset( $links ) );
		$this->assertStringContainsString( 'Ajustes', (string) reset( $links ) );
	}
}
