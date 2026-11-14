<?php
/**
 * Plugin Name: SportIQ Account Privacy Requests
 * Description: Logged-in member privacy request intake without automatic account deletion.
 * Version: 0.2.0
 * Requires PHP: 7.4
 */
if (!defined('ABSPATH')) exit;

function sportiq_privacy_request_shortcode() {
    if (!is_user_logged_in()) {
        return '<p>계정 관련 요청은 <a href="' . esc_url(wp_login_url(get_permalink())) . '">로그인</a> 후 제출할 수 있습니다.</p>';
    }
    $user_id = get_current_user_id();
    $notice = '';
    if (isset($_POST['sportiq_privacy_submit'])) {
        $nonce = isset($_POST['sportiq_privacy_nonce']) ? sanitize_text_field(wp_unslash($_POST['sportiq_privacy_nonce'])) : '';
        if (!wp_verify_nonce($nonce, 'sportiq_privacy_request_' . $user_id)) {
            $notice = '요청 확인에 실패했습니다. 페이지를 새로고침한 후 다시 시도하세요.';
        } else {
            $kind = isset($_POST['sportiq_privacy_kind']) ? sanitize_key(wp_unslash($_POST['sportiq_privacy_kind'])) : '';
            if (!in_array($kind, array('access', 'export', 'erase'), true)) {
                $notice = '요청 유형을 확인하세요.';
            } else {
                $last = (int) get_user_meta($user_id, 'sportiq_privacy_last_request', true);
                if ($last && time() - $last < HOUR_IN_SECONDS) {
                    $notice = '최근 요청이 접수되었습니다. 잠시 후 다시 시도하세요.';
                } else {
                    $request_id = wp_insert_post(array(
                        'post_type' => 'sportiq_privacy_req',
                        'post_status' => 'private',
                        'post_title' => 'SportIQ privacy request: ' . $kind . ' #' . $user_id,
                        'post_author' => $user_id,
                        'post_content' => '',
                        'meta_input' => array(
                            '_sportiq_request_kind' => $kind,
                            '_sportiq_request_status' => 'pending',
                            '_sportiq_request_user_id' => $user_id,
                        ),
                    ), true);
                    if (is_wp_error($request_id) || !$request_id) {
                        $notice = '요청을 저장하지 못했습니다. 관리자에게 문의하세요.';
                    } else {
                        update_user_meta($user_id, 'sportiq_privacy_last_request', time());
                        $notice = '요청이 접수되었습니다. 접수 번호: ' . absint($request_id) . '. 요청 접수만으로 계정이 삭제되지는 않습니다.';
                    }
                }
            }
        }
    }
    $html = '<section class="sportiq-privacy-request"><h2>내 개인정보 요청</h2>';
    if ($notice) $html .= '<p role="status">' . esc_html($notice) . '</p>';
    $html .= '<p>이 양식은 요청 접수용입니다. 본인 확인 및 운영자 검토 후 처리되며 계정은 자동 삭제되지 않습니다.</p>';
    $html .= '<form method="post">' . wp_nonce_field('sportiq_privacy_request_' . $user_id, 'sportiq_privacy_nonce', true, false);
    $html .= '<label for="sportiq-privacy-kind">요청 유형</label> <select id="sportiq-privacy-kind" name="sportiq_privacy_kind">';
    $html .= '<option value="access">개인정보 열람</option><option value="export">개인정보 내보내기</option><option value="erase">계정 및 개인정보 삭제 요청</option>';
    $html .= '</select> <button type="submit" name="sportiq_privacy_submit" value="1">요청 접수</button></form></section>';
    return $html;
}
function sportiq_privacy_request_register_type() {
    register_post_type('sportiq_privacy_req', array(
        'label' => 'SportIQ 개인정보 요청',
        'public' => false,
        'show_ui' => true,
        'show_in_menu' => true,
        'show_in_rest' => false,
        'supports' => array('title', 'author'),
        'capabilities' => array(
            'edit_post' => 'manage_options',
            'read_post' => 'manage_options',
            'delete_post' => 'manage_options',
            'edit_posts' => 'manage_options',
            'edit_others_posts' => 'manage_options',
            'publish_posts' => 'manage_options',
            'read_private_posts' => 'manage_options',
            'delete_posts' => 'manage_options',
            'delete_private_posts' => 'manage_options',
        ),
        'map_meta_cap' => false,
    ));
}
add_action('init', 'sportiq_privacy_request_register_type');
add_shortcode('sportiq_privacy_request', 'sportiq_privacy_request_shortcode');


/**
 * Include SportIQ favorites in the WordPress core personal-data export workflow.
 * Uses only the requested user's email and never exposes another member's data.
 */
function sportiq_privacy_export_favorites($email_address, $page = 1) {
    if ((int) $page !== 1) return array('data' => array(), 'done' => true);
    $user = get_user_by('email', $email_address);
    if (!$user) return array('data' => array(), 'done' => true);
    $favorites = get_user_meta($user->ID, 'sportiq_favorites_v1', true);
    if (!is_array($favorites) || !$favorites) {
        return array('data' => array(), 'done' => true);
    }
    $items = array();
    foreach ($favorites as $key => $value) {
        // Preserve the original stored structure without exporting unrelated user meta.
        $items[] = array(
            'name' => is_string($key) ? $key : '관심 목록 항목',
            'value' => is_scalar($value) ? (string) $value : wp_json_encode($value, JSON_UNESCAPED_UNICODE),
        );
    }
    return array(
        'data' => array(array(
            'group_id' => 'sportiq-favorites',
            'group_label' => 'SportIQ 관심 목록',
            'item_id' => 'sportiq-favorites-' . $user->ID,
            'data' => $items,
        )),
        'done' => true,
    );
}
function sportiq_privacy_register_exporter($exporters) {
    $exporters['sportiq-favorites'] = array(
        'exporter_friendly_name' => 'SportIQ 관심 팀·리그',
        'callback' => 'sportiq_privacy_export_favorites',
    );
    return $exporters;
}
add_filter('wp_privacy_personal_data_exporters', 'sportiq_privacy_register_exporter');

/**
 * WordPress core invokes erasers only during its separate verified erasure
 * workflow; the SportIQ request form itself does not invoke this callback.
 */
function sportiq_privacy_erase_favorites($email_address, $page = 1) {
    if ((int) $page !== 1) {
        return array('items_removed' => false, 'items_retained' => false, 'messages' => array(), 'done' => true);
    }
    $user = get_user_by('email', $email_address);
    if (!$user) {
        return array('items_removed' => false, 'items_retained' => false, 'messages' => array(), 'done' => true);
    }
    $exists = metadata_exists('user', $user->ID, 'sportiq_favorites_v1');
    if (!$exists) {
        return array('items_removed' => false, 'items_retained' => false, 'messages' => array(), 'done' => true);
    }
    $removed = delete_user_meta($user->ID, 'sportiq_favorites_v1');
    return array(
        'items_removed' => (bool) $removed,
        'items_retained' => !$removed,
        'messages' => $removed ? array() : array('SportIQ 관심 목록을 삭제하지 못했습니다. 관리자가 확인해야 합니다.'),
        'done' => true,
    );
}
function sportiq_privacy_register_eraser($erasers) {
    $erasers['sportiq-favorites'] = array(
        'eraser_friendly_name' => 'SportIQ 관심 팀·리그',
        'callback' => 'sportiq_privacy_erase_favorites',
    );
    return $erasers;
}
add_filter('wp_privacy_personal_data_erasers', 'sportiq_privacy_register_eraser');
