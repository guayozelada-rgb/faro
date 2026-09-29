<?php
/**
 * Cifrado del secreto HMAC y utilidades base64url.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Cifra el secreto HMAC con sodium_crypto_secretbox y una llave derivada de wp_salt( 'auth' )
 * (ADR 0011 §4). Un volcado de la base sin wp-config.php no permite descifrarlo.
 */
final class Faro_Crypto {

	/**
	 * Prefijo de versión del formato guardado.
	 */
	private const PREFIX = 'v1:';

	/**
	 * Contexto de derivación de la llave.
	 */
	private const KEY_CONTEXT = 'faro-hmac-secret-v1';

	/**
	 * Codifica en base64url sin relleno.
	 *
	 * @param string $bytes Bytes.
	 * @return string
	 */
	public static function base64url_encode( string $bytes ): string {
		return rtrim( strtr( base64_encode( $bytes ), '+/', '-_' ), '=' ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode -- Codificación de datos binarios, no ofuscación.
	}

	/**
	 * Decodifica base64url sin relleno. Devuelve null si el texto no es válido.
	 *
	 * @param string $text Texto base64url.
	 * @return string|null
	 */
	public static function base64url_decode( string $text ): ?string {
		if ( 1 !== preg_match( '/^[A-Za-z0-9_-]*$/', $text ) || 1 === strlen( $text ) % 4 ) {
			return null;
		}
		$padded = str_pad( strtr( $text, '-_', '+/' ), (int) ( ceil( strlen( $text ) / 4 ) * 4 ), '=' );
		$bytes  = base64_decode( $padded, true ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_decode -- Decodificación de datos binarios, no ofuscación.

		return false === $bytes ? null : $bytes;
	}

	/**
	 * Genera 32 bytes aleatorios.
	 *
	 * @return string
	 */
	public static function random_key(): string {
		return random_bytes( 32 );
	}

	/**
	 * Cifra un valor para guardarlo en la base.
	 *
	 * @param string $plain Bytes en claro.
	 * @return string Formato "v1:" + base64( nonce de 24 bytes || texto cifrado ).
	 */
	public static function encrypt( string $plain ): string {
		$nonce  = random_bytes( SODIUM_CRYPTO_SECRETBOX_NONCEBYTES );
		$cipher = sodium_crypto_secretbox( $plain, $nonce, self::key() );

		return self::PREFIX . base64_encode( $nonce . $cipher ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode -- Codificación de datos binarios, no ofuscación.
	}

	/**
	 * Descifra un valor guardado. Devuelve null si no se puede (por ejemplo, porque cambiaron las salts).
	 *
	 * @param string $stored Valor guardado por encrypt().
	 * @return string|null
	 */
	public static function decrypt( string $stored ): ?string {
		if ( ! str_starts_with( $stored, self::PREFIX ) ) {
			return null;
		}
		$raw = base64_decode( substr( $stored, strlen( self::PREFIX ) ), true ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_decode -- Decodificación de datos binarios, no ofuscación.
		if ( false === $raw || strlen( $raw ) <= SODIUM_CRYPTO_SECRETBOX_NONCEBYTES + SODIUM_CRYPTO_SECRETBOX_MACBYTES ) {
			return null;
		}
		$nonce  = substr( $raw, 0, SODIUM_CRYPTO_SECRETBOX_NONCEBYTES );
		$cipher = substr( $raw, SODIUM_CRYPTO_SECRETBOX_NONCEBYTES );

		try {
			$plain = sodium_crypto_secretbox_open( $cipher, $nonce, self::key() );
		} catch ( Throwable $e ) {
			return null;
		}

		return false === $plain ? null : $plain;
	}

	/**
	 * Llave de 32 bytes derivada de wp_salt( 'auth' ).
	 *
	 * @return string
	 */
	private static function key(): string {
		return hash_hmac( 'sha256', self::KEY_CONTEXT, wp_salt( 'auth' ), true );
	}
}
