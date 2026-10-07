<?php
/**
 * Pruebas del cifrado y de la desinstalación.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Faro_Crypto y uninstall.php.
 */
class Test_Faro_Crypto_Uninstall extends Faro_Test_Case {

	public function test_encrypt_round_trip_and_format(): void {
		$plain  = random_bytes( 32 );
		$stored = Faro_Crypto::encrypt( $plain );

		$this->assertStringStartsWith( 'v1:', $stored );
		$raw = base64_decode( substr( $stored, 3 ), true ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_decode -- Comprobación del formato.
		$this->assertSame( 24 + 16 + 32, strlen( (string) $raw ) );
		$this->assertSame( $plain, Faro_Crypto::decrypt( $stored ) );
		$this->assertNotSame( $stored, Faro_Crypto::encrypt( $plain ), 'Cada cifrado usa un nonce nuevo.' );
	}

	public function test_decrypt_fails_with_other_salts_or_tampering(): void {
		$stored = Faro_Crypto::encrypt( 'secreto-de-prueba' );

		// Se invierte un bit del último byte: siempre cambia el texto cifrado (poner un byte fijo
		// fallaba 1 de cada 256 veces, cuando ese byte ya tenía ese valor).
		$raw      = (string) base64_decode( substr( $stored, 3 ) ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions -- Alteración de prueba.
		$raw[-1]  = chr( ord( $raw[-1] ) ^ 0x01 );
		$tampered = 'v1:' . base64_encode( $raw ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions -- Alteración de prueba.
		$this->assertNull( Faro_Crypto::decrypt( $tampered ) );
		$this->assertNull( Faro_Crypto::decrypt( 'v2:abc' ) );
		$this->assertNull( Faro_Crypto::decrypt( 'v1:corto' ) );

		add_filter(
			'salt',
			static function ( $salt, $scheme ) {
				return 'auth' === $scheme ? 'otras-salts' : $salt;
			},
			10,
			2
		);
		$this->assertNull( Faro_Crypto::decrypt( $stored ) );
	}

	public function test_base64url(): void {
		$bytes = "\xfb\xff\xfe" . random_bytes( 29 );
		$text  = Faro_Crypto::base64url_encode( $bytes );

		$this->assertMatchesRegularExpression( '/^[A-Za-z0-9_-]{43}$/', $text );
		$this->assertSame( $bytes, Faro_Crypto::base64url_decode( $text ) );
		$this->assertNull( Faro_Crypto::base64url_decode( 'no+válido' ) );
		$this->assertNull( Faro_Crypto::base64url_decode( 'abcde' ) );
	}

	public function test_uninstall_removes_all_faro_options_and_transients(): void {
		global $wpdb;
		$this->pair();
		Faro_Pairing::create_code();
		set_transient( 'faro_nonce_' . hash( 'sha256', 'x' ), 1, 600 );
		set_transient(
			'faro_pair_ip_' . hash( 'sha256', 'y' ),
			array(
				'count' => 1,
				'until' => time() + 900,
			),
			900
		);
		update_option( 'faro_otra_opcion', 'x' );
		update_option( 'otro_plugin_opcion', 'se queda' );

		if ( ! defined( 'WP_UNINSTALL_PLUGIN' ) ) {
			define( 'WP_UNINSTALL_PLUGIN', 'faro/faro.php' );
		}
		include dirname( __DIR__ ) . '/uninstall.php';

		$left = $wpdb->get_col( // phpcs:ignore WordPress.DB.DirectDatabaseQuery
			$wpdb->prepare(
				"SELECT option_name FROM {$wpdb->options} WHERE option_name LIKE %s OR option_name LIKE %s OR option_name LIKE %s",
				'faro_connection',
				'%faro_nonce_%',
				'%faro_pair%'
			)
		);
		$this->assertSame( array(), $left );
		$this->assertSame( 'se queda', get_option( 'otro_plugin_opcion' ) );
		$this->assertSame( 'x', get_option( 'faro_otra_opcion' ), 'Solo se borran las opciones y transients propios, no cualquier faro_*.' );
	}

	public function test_salt_configuration_check(): void {
		$this->assertTrue( Faro_Crypto::is_configured_salt( 'una-frase-larga-y-unica' ) );
		$this->assertFalse( Faro_Crypto::is_configured_salt( null ) );
		$this->assertFalse( Faro_Crypto::is_configured_salt( '' ) );
		$this->assertFalse( Faro_Crypto::is_configured_salt( '   ' ) );
		$this->assertFalse( Faro_Crypto::is_configured_salt( 'put your unique phrase here' ) );
		$this->assertSame(
			Faro_Crypto::is_configured_salt( defined( 'AUTH_KEY' ) ? AUTH_KEY : null ) && Faro_Crypto::is_configured_salt( defined( 'AUTH_SALT' ) ? AUTH_SALT : null ),
			Faro_Crypto::salts_from_config()
		);
	}
}
