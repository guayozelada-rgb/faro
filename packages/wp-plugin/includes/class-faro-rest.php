<?php
/**
 * Rutas REST /wp-json/faro/v1/.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Registra las rutas de la API. Todas tienen permission_callback: la vinculación
 * (código pendiente + límite por IP) o la firma HMAC. Ninguna usa __return_true.
 */
final class Faro_Rest {

	/**
	 * Espacio de nombres.
	 */
	public const NAMESPACE = 'faro/v1';

	/**
	 * Elementos por página por defecto y máximo.
	 */
	public const DEFAULT_PER_PAGE = 50;
	public const MAX_PER_PAGE     = 100;

	/**
	 * Página máxima aceptada (evita desplazamientos enormes en la base).
	 */
	public const MAX_PAGE = 100000;

	/**
	 * Registra las rutas.
	 *
	 * @return void
	 */
	public static function register_routes(): void {
		register_rest_route(
			self::NAMESPACE,
			'/pair',
			array(
				'methods'             => WP_REST_Server::CREATABLE,
				'callback'            => array( self::class, 'pair' ),
				'permission_callback' => array( Faro_Pairing::class, 'permission' ),
				'args'                => array(
					'code'            => array(
						'type'              => 'string',
						'required'          => true,
						'validate_callback' => array( self::class, 'validate_code' ),
						'sanitize_callback' => 'sanitize_text_field',
					),
					'app_instance_id' => array(
						'type'              => 'string',
						'required'          => true,
						'validate_callback' => array( self::class, 'validate_uuid' ),
						'sanitize_callback' => 'sanitize_text_field',
					),
					'app_version'     => array(
						'type'              => 'string',
						'required'          => true,
						'validate_callback' => array( self::class, 'validate_app_version' ),
						'sanitize_callback' => 'sanitize_text_field',
					),
				),
			)
		);

		register_rest_route(
			self::NAMESPACE,
			'/status',
			array(
				'methods'             => WP_REST_Server::READABLE,
				'callback'            => array( self::class, 'status' ),
				'permission_callback' => array( Faro_Signature::class, 'permission' ),
			)
		);

		foreach ( Faro_Content::KINDS as $kind ) {
			register_rest_route(
				self::NAMESPACE,
				'/' . $kind,
				array(
					'methods'             => WP_REST_Server::READABLE,
					'callback'            => array( self::class, 'list_content' ),
					'permission_callback' => array( Faro_Signature::class, 'permission' ),
					'args'                => array(
						'page'     => array(
							'type'              => 'integer',
							'default'           => 1,
							'validate_callback' => array( self::class, 'validate_page' ),
							'sanitize_callback' => 'absint',
						),
						'per_page' => array(
							'type'              => 'integer',
							'default'           => self::DEFAULT_PER_PAGE,
							'validate_callback' => array( self::class, 'validate_per_page' ),
							'sanitize_callback' => 'absint',
						),
					),
				)
			);
		}

		register_rest_route(
			self::NAMESPACE,
			'/connection',
			array(
				'methods'             => WP_REST_Server::DELETABLE,
				'callback'            => array( self::class, 'revoke' ),
				'permission_callback' => array( Faro_Signature::class, 'permission' ),
			)
		);
	}

	/**
	 * POST /pair: canjea el código y entrega las credenciales una sola vez.
	 *
	 * @param WP_REST_Request $request Petición.
	 * @return WP_REST_Response|WP_Error
	 *
	 * @phpstan-param WP_REST_Request<array<string, mixed>> $request
	 */
	public static function pair( WP_REST_Request $request ) {
		$redeemed = Faro_Pairing::redeem( (string) $request->get_param( 'code' ) );
		if ( is_wp_error( $redeemed ) ) {
			return $redeemed;
		}
		$credentials = Faro_Connection::create(
			(string) $request->get_param( 'app_instance_id' ),
			(string) $request->get_param( 'app_version' )
		);

		return new WP_REST_Response(
			array(
				'api_version'   => FARO_API_VERSION,
				'connection_id' => $credentials['connection_id'],
				'token'         => $credentials['token'],
				'hmac_secret'   => $credentials['hmac_secret'],
				'site'          => Faro_Status::site(),
			),
			200
		);
	}

	/**
	 * GET /status.
	 *
	 * @return WP_REST_Response
	 */
	public static function status(): WP_REST_Response {
		return new WP_REST_Response( Faro_Status::get(), 200 );
	}

	/**
	 * GET /pages, /posts, /products.
	 *
	 * @param WP_REST_Request $request Petición.
	 * @return WP_REST_Response
	 *
	 * @phpstan-param WP_REST_Request<array<string, mixed>> $request
	 */
	public static function list_content( WP_REST_Request $request ): WP_REST_Response {
		$kind = ltrim( substr( untrailingslashit( $request->get_route() ), strlen( '/' . self::NAMESPACE ) ), '/' );
		$page = Faro_Content::get_page(
			in_array( $kind, Faro_Content::KINDS, true ) ? $kind : 'posts',
			max( 1, absint( $request->get_param( 'page' ) ) ),
			max( 1, min( self::MAX_PER_PAGE, absint( $request->get_param( 'per_page' ) ) ) )
		);

		return new WP_REST_Response( $page, 200 );
	}

	/**
	 * DELETE /connection: revoca la conexión desde la app.
	 *
	 * @return WP_REST_Response
	 */
	public static function revoke(): WP_REST_Response {
		Faro_Connection::revoke();

		return new WP_REST_Response( array( 'revoked' => true ), 200 );
	}

	/**
	 * Valida el código: exactamente 6 dígitos.
	 *
	 * @param mixed $value Valor.
	 * @return bool
	 */
	public static function validate_code( $value ): bool {
		return is_string( $value ) && 1 === preg_match( '/^[0-9]{6}$/', $value );
	}

	/**
	 * Valida un UUID.
	 *
	 * @param mixed $value Valor.
	 * @return bool
	 */
	public static function validate_uuid( $value ): bool {
		return is_string( $value ) && wp_is_uuid( $value );
	}

	/**
	 * Valida la versión de la app.
	 *
	 * @param mixed $value Valor.
	 * @return bool
	 */
	public static function validate_app_version( $value ): bool {
		return is_string( $value ) && 1 === preg_match( '/^[0-9A-Za-z.+-]{1,32}$/', $value );
	}

	/**
	 * Valida el número de página.
	 *
	 * @param mixed $value Valor.
	 * @return bool
	 */
	public static function validate_page( $value ): bool {
		return self::is_int_in_range( $value, 1, self::MAX_PAGE );
	}

	/**
	 * Valida los elementos por página.
	 *
	 * @param mixed $value Valor.
	 * @return bool
	 */
	public static function validate_per_page( $value ): bool {
		return self::is_int_in_range( $value, 1, self::MAX_PER_PAGE );
	}

	/**
	 * Convierte los errores de parámetros de WordPress de las rutas faro/v1 en wp.invalid_input,
	 * pero solo después de comprobar la autorización (una petición sin firma recibe 401, no 400).
	 *
	 * @param mixed                $response Respuesta o error hasta ahora.
	 * @param array<string, mixed> $handler  Manejador de la ruta.
	 * @param mixed                $request  Petición (WP_REST_Request).
	 * @return mixed
	 */
	public static function before_callbacks( $response, $handler, $request ) {
		if ( ! is_wp_error( $response ) || ! $request instanceof WP_REST_Request || ! self::is_faro_route( $request->get_route() ) ) {
			return $response;
		}
		if ( ! in_array( $response->get_error_code(), array( 'rest_invalid_param', 'rest_missing_callback_param', 'rest_invalid_json' ), true ) ) {
			return $response;
		}
		if ( isset( $handler['permission_callback'] ) && is_callable( $handler['permission_callback'] ) ) {
			$permission = call_user_func( $handler['permission_callback'], $request );
			if ( is_wp_error( $permission ) ) {
				return $permission;
			}
			if ( true !== $permission ) {
				return Faro_Errors::get( 'wp.invalid_signature' );
			}
		}

		return Faro_Errors::get( 'wp.invalid_input' );
	}

	/**
	 * Añade Cache-Control: no-store, private a todas las respuestas faro/v1.
	 *
	 * @param mixed $response Respuesta.
	 * @param mixed $server   Servidor REST.
	 * @param mixed $request  Petición (WP_REST_Request).
	 * @return mixed
	 */
	public static function no_store( $response, $server, $request ) {
		unset( $server );
		if ( $response instanceof WP_HTTP_Response && $request instanceof WP_REST_Request && self::is_faro_route( $request->get_route() ) ) {
			$response->header( 'Cache-Control', 'no-store, private' );
		}

		return $response;
	}

	/**
	 * Indica si la ruta pertenece a faro/v1.
	 *
	 * @param string $route Ruta REST.
	 * @return bool
	 */
	public static function is_faro_route( string $route ): bool {
		$route = untrailingslashit( $route );

		return '/' . self::NAMESPACE === $route || str_starts_with( $route, '/' . self::NAMESPACE . '/' );
	}

	/**
	 * Comprueba que un valor es un entero (o texto de dígitos) dentro de un rango.
	 *
	 * @param mixed $value Valor.
	 * @param int   $min   Mínimo.
	 * @param int   $max   Máximo.
	 * @return bool
	 */
	private static function is_int_in_range( $value, int $min, int $max ): bool {
		if ( is_int( $value ) ) {
			$number = $value;
		} elseif ( is_string( $value ) && 1 === preg_match( '/^[0-9]{1,9}$/', $value ) ) {
			$number = (int) $value;
		} else {
			return false;
		}

		return $number >= $min && $number <= $max;
	}
}
