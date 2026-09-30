<?php
/**
 * Pantalla Ajustes → Faro.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Muestra el estado de la conexión, genera el código de vinculación y permite desconectar.
 * Solo usuarios con manage_options; toda acción con nonce.
 */
final class Faro_Admin {

	/**
	 * Slug de la página.
	 */
	public const PAGE_SLUG = 'faro';

	/**
	 * Capacidad exigida.
	 */
	public const CAPABILITY = 'manage_options';

	/**
	 * Código generado en esta petición (solo se muestra en la respuesta a esa acción).
	 *
	 * @var string|null
	 */
	private static ?string $code = null;

	/**
	 * Aviso del resultado de la acción: "disconnected", "insecure" o null.
	 *
	 * @var string|null
	 */
	private static ?string $notice = null;

	/**
	 * Registra los hooks de administración.
	 *
	 * @return void
	 */
	public static function init(): void {
		add_action( 'admin_menu', array( self::class, 'add_page' ) );
		add_filter( 'plugin_action_links_' . plugin_basename( FARO_PLUGIN_FILE ), array( self::class, 'action_links' ) );
	}

	/**
	 * Añade la página en Ajustes.
	 *
	 * @return void
	 */
	public static function add_page(): void {
		$hook = add_options_page(
			__( 'Faro', 'faro' ),
			__( 'Faro', 'faro' ),
			self::CAPABILITY,
			self::PAGE_SLUG,
			array( self::class, 'render' )
		);
		if ( false !== $hook ) {
			add_action( 'load-' . $hook, array( self::class, 'handle_post' ) );
		}
	}

	/**
	 * Enlace "Ajustes" en la lista de plugins.
	 *
	 * @param array<int|string, string> $links Enlaces.
	 * @return array<int|string, string>
	 */
	public static function action_links( $links ): array {
		$settings = sprintf(
			'<a href="%s">%s</a>',
			esc_url( admin_url( 'options-general.php?page=' . self::PAGE_SLUG ) ),
			esc_html__( 'Ajustes', 'faro' )
		);
		array_unshift( $links, $settings );

		return $links;
	}

	/**
	 * Procesa los formularios de la página (generar código y desconectar).
	 *
	 * @return void
	 */
	public static function handle_post(): void {
		$method = isset( $_SERVER['REQUEST_METHOD'] ) ? sanitize_text_field( wp_unslash( $_SERVER['REQUEST_METHOD'] ) ) : '';
		if ( 'POST' !== $method || ! isset( $_POST['faro_action'] ) ) { // phpcs:ignore WordPress.Security.NonceVerification.Missing -- El nonce se verifica abajo, según la acción.
			return;
		}
		if ( ! current_user_can( self::CAPABILITY ) ) {
			wp_die( esc_html__( 'No tienes permiso para hacer esto.', 'faro' ), '', array( 'response' => 403 ) );
		}
		$action = sanitize_key( wp_unslash( $_POST['faro_action'] ) ); // phpcs:ignore WordPress.Security.NonceVerification.Missing -- El nonce se verifica abajo, según la acción.

		if ( 'generate_code' === $action ) {
			check_admin_referer( 'faro_generate_code' );
			if ( ! Faro_Pairing::site_allows_pairing() ) {
				self::$notice = 'insecure';
				return;
			}
			self::$code = Faro_Pairing::create_code();
			return;
		}

		if ( 'disconnect' === $action ) {
			check_admin_referer( 'faro_disconnect' );
			Faro_Connection::revoke();
			self::$notice = 'disconnected';
			return;
		}

		wp_die( esc_html__( 'Acción no válida.', 'faro' ), '', array( 'response' => 400 ) );
	}

	/**
	 * Código generado en esta petición (para pruebas y la vista).
	 *
	 * @return string|null
	 */
	public static function generated_code(): ?string {
		return self::$code;
	}

	/**
	 * Aviso de la acción de esta petición.
	 *
	 * @return string|null
	 */
	public static function notice(): ?string {
		return self::$notice;
	}

	/**
	 * Limpia el estado de la petición (solo pruebas).
	 *
	 * @return void
	 */
	public static function reset(): void {
		self::$code   = null;
		self::$notice = null;
	}

	/**
	 * Muestra la página.
	 *
	 * @return void
	 */
	public static function render(): void {
		if ( ! current_user_can( self::CAPABILITY ) ) {
			wp_die( esc_html__( 'No tienes permiso para ver esta página.', 'faro' ), '', array( 'response' => 403 ) );
		}

		$faro_connection  = Faro_Connection::get();
		$faro_broken      = null !== $faro_connection && null === Faro_Connection::secret( $faro_connection );
		$faro_secure      = Faro_Pairing::site_allows_pairing();
		$faro_salts_ok    = Faro_Crypto::salts_from_config();
		$faro_code        = self::$code;
		$faro_notice      = self::$notice;
		$faro_date_format = get_option( 'date_format' ) . ' ' . get_option( 'time_format' );

		include FARO_PLUGIN_DIR . 'admin/views/settings-page.php';
	}
}
