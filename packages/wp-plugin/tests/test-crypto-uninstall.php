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

		$tampered = 'v1:' . base64_encode( substr( (string) base64_decode( substr( $stored, 3 ) ), 0, -1 ) . 'x' ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions -- Alteración de prueba.
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
				'%faro\_%',
				'%faro_nonce%',
				'%faro_pair%'
			)
		);
		$this->assertSame( array(), $left );
		$this->assertSame( 'se queda', get_option( 'otro_plugin_opcion' ) );
	}
}
