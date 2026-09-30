<?php
/**
 * Plugin Name:          Faro
 * Description:          Conecta tu sitio con Faro, la app de escritorio de marketing digital, para que pueda leer tus páginas, entradas y productos. No necesita tu contraseña.
 * Version:              0.1.0
 * Requires at least:    6.0
 * Requires PHP:         8.1
 * Author:               Concersa
 * License:              GPL-2.0-or-later
 * License URI:          https://www.gnu.org/licenses/gpl-2.0.html
 * Text Domain:          faro
 * Domain Path:          /languages
 * WC requires at least: 11.1
 * WC tested up to:      11.1
 *
 * Copyright (C) 2026 Concersa
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU General Public License for more details.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

define( 'FARO_VERSION', '0.1.0' );
define( 'FARO_API_VERSION', 1 );
define( 'FARO_PLUGIN_FILE', __FILE__ );
define( 'FARO_PLUGIN_DIR', plugin_dir_path( __FILE__ ) );

require_once FARO_PLUGIN_DIR . 'includes/class-faro-clock.php';
require_once FARO_PLUGIN_DIR . 'includes/class-faro-errors.php';
require_once FARO_PLUGIN_DIR . 'includes/class-faro-crypto.php';
require_once FARO_PLUGIN_DIR . 'includes/class-faro-connection.php';
require_once FARO_PLUGIN_DIR . 'includes/class-faro-pairing.php';
require_once FARO_PLUGIN_DIR . 'includes/class-faro-signature.php';
require_once FARO_PLUGIN_DIR . 'includes/woocommerce/class-faro-woocommerce.php';
require_once FARO_PLUGIN_DIR . 'includes/seo/class-faro-seo-detector.php';
require_once FARO_PLUGIN_DIR . 'includes/class-faro-content.php';
require_once FARO_PLUGIN_DIR . 'includes/class-faro-status.php';
require_once FARO_PLUGIN_DIR . 'includes/class-faro-rest.php';
require_once FARO_PLUGIN_DIR . 'includes/class-faro-plugin.php';
require_once FARO_PLUGIN_DIR . 'admin/class-faro-admin.php';

Faro_Plugin::init();
