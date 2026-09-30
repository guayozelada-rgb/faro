<?php
/**
 * Vista de Ajustes → Faro.
 *
 * Variables (desde Faro_Admin::render()):
 *
 * @var array<string, mixed>|null $faro_connection  Conexión guardada.
 * @var bool                      $faro_broken      La conexión no se puede descifrar (salts cambiadas).
 * @var bool                      $faro_secure      El sitio permite vincular (HTTPS o entorno local).
 * @var bool                      $faro_salts_ok    AUTH_KEY y AUTH_SALT vienen de wp-config.php.
 * @var string|null               $faro_code        Código recién generado.
 * @var string|null               $faro_notice      Aviso de la acción.
 * @var string                    $faro_date_format Formato de fecha y hora.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;
?>
<div class="wrap">
	<h1><?php esc_html_e( 'Faro', 'faro' ); ?></h1>

	<?php if ( 'disconnected' === $faro_notice ) : ?>
		<div class="notice notice-success"><p><?php esc_html_e( 'Desconectamos tu sitio de Faro.', 'faro' ); ?></p></div>
	<?php endif; ?>

	<?php if ( ! $faro_secure ) : ?>
		<div class="notice notice-warning inline"><p><?php esc_html_e( 'Tu sitio no usa HTTPS. Faro solo se conecta a sitios con HTTPS.', 'faro' ); ?></p></div>
	<?php endif; ?>

	<?php if ( ! $faro_salts_ok ) : ?>
		<div class="notice notice-warning inline"><p><?php esc_html_e( 'Las claves de seguridad AUTH_KEY y AUTH_SALT no están definidas en wp-config.php. Conviene definirlas: así, una copia de tu base de datos no basta para usar la conexión con Faro.', 'faro' ); ?></p></div>
	<?php endif; ?>

	<?php if ( $faro_broken ) : ?>
		<div class="notice notice-error inline"><p><?php esc_html_e( 'La conexión con Faro dejó de funcionar porque cambiaron las claves de seguridad de WordPress. Genera un código nuevo y escríbelo en Faro.', 'faro' ); ?></p></div>
	<?php endif; ?>

	<?php if ( null !== $faro_code ) : ?>
		<div class="card">
			<h2><?php esc_html_e( 'Tu código de conexión', 'faro' ); ?></h2>
			<p style="font-size: 2.5em; font-weight: 600; letter-spacing: 0.1em; font-family: monospace;"><?php echo esc_html( substr( $faro_code, 0, 3 ) . ' ' . substr( $faro_code, 3 ) ); ?></p>
			<p>
				<?php
				/* translators: %s: dirección del sitio. */
				printf( esc_html__( 'Escríbelo en Faro junto con esta dirección: %s', 'faro' ), '<code>' . esc_html( untrailingslashit( home_url() ) ) . '</code>' );
				?>
			</p>
			<p><?php esc_html_e( 'Caduca en 10 minutos y solo sirve una vez.', 'faro' ); ?></p>
			<?php if ( null !== $faro_connection ) : ?>
				<p><strong><?php esc_html_e( 'Si usas este código, la conexión actual se reemplazará.', 'faro' ); ?></strong></p>
			<?php endif; ?>
			<form method="post">
				<?php wp_nonce_field( 'faro_generate_code' ); ?>
				<input type="hidden" name="faro_action" value="generate_code" />
				<?php submit_button( __( 'Generar otro código', 'faro' ), 'secondary', 'submit', false ); ?>
			</form>
		</div>
	<?php elseif ( null !== $faro_connection && ! $faro_broken ) : ?>
		<div class="card">
			<p>
				<?php
				/* translators: %s: fecha y hora de la conexión. */
				printf( esc_html__( 'Tu sitio está conectado con Faro desde el %s.', 'faro' ), esc_html( (string) wp_date( $faro_date_format, (int) $faro_connection['created_at'] ) ) );
				?>
			</p>
			<p>
				<?php
				/* translators: %s: fecha y hora de la última lectura. */
				printf( esc_html__( 'Última lectura de Faro: %s.', 'faro' ), esc_html( (string) wp_date( $faro_date_format, (int) $faro_connection['last_seen_at'] ) ) );
				?>
			</p>
			<form method="post" style="display: inline-block; margin-right: 8px;">
				<?php wp_nonce_field( 'faro_disconnect' ); ?>
				<input type="hidden" name="faro_action" value="disconnect" />
				<?php
				submit_button(
					__( 'Desconectar de Faro', 'faro' ),
					'primary',
					'submit',
					false,
					array( 'onclick' => 'return window.confirm(' . wp_json_encode( __( 'Faro dejará de leer este sitio. ¿Quieres desconectarlo?', 'faro' ) ) . ');' )
				);
				?>
			</form>
			<form method="post" style="display: inline-block;">
				<?php wp_nonce_field( 'faro_generate_code' ); ?>
				<input type="hidden" name="faro_action" value="generate_code" />
				<?php submit_button( __( 'Generar código de conexión', 'faro' ), 'secondary', 'submit', false, $faro_secure ? '' : array( 'disabled' => 'disabled' ) ); ?>
			</form>
		</div>
	<?php else : ?>
		<div class="card">
			<h2><?php esc_html_e( 'Conecta tu sitio con Faro', 'faro' ); ?></h2>
			<p><?php esc_html_e( 'Faro leerá tus páginas, entradas y productos. No necesita tu contraseña.', 'faro' ); ?></p>
			<form method="post">
				<?php wp_nonce_field( 'faro_generate_code' ); ?>
				<input type="hidden" name="faro_action" value="generate_code" />
				<?php submit_button( __( 'Generar código de conexión', 'faro' ), 'primary', 'submit', false, $faro_secure ? '' : array( 'disabled' => 'disabled' ) ); ?>
			</form>
		</div>
	<?php endif; ?>

	<p><?php esc_html_e( 'Abre Faro en tu computadora y ve a Configuración → Sitios conectados.', 'faro' ); ?></p>
</div>
