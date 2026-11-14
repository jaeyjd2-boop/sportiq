<?php
/**
 * Plugin Name: SportIQ Member Favorites
 * Description: Logged-in users can save favorite football teams and leagues.
 * Version: 0.1.0
 * Requires PHP: 7.4
 */
if (!defined('ABSPATH')) { exit; }

function sportiq_favorites_options() {
    return array('team' => '관심 팀', 'league' => '관심 리그');
}
function sportiq_favorites_read($user_id) {
    $saved = get_user_meta($user_id, 'sportiq_favorites_v1', true);
    return is_array($saved) ? $saved : array();
}
function sportiq_favorites_shortcode() {
    if (!is_user_logged_in()) {
        return '<p>관심 팀을 저장하려면 먼저 <a href="' . esc_url(wp_login_url(get_permalink())) . '">로그인</a>하세요.</p>';
    }
    $uid = get_current_user_id();
    $message = '';
    if (isset($_POST['sportiq_fav_action'])) {
        $nonce = isset($_POST['sportiq_fav_nonce']) ? sanitize_text_field(wp_unslash($_POST['sportiq_fav_nonce'])) : '';
        if (!wp_verify_nonce($nonce, 'sportiq_favorites_' . $uid)) {
            $message = '<p role="alert">보안 검증에 실패했습니다. 다시 시도하세요.</p>';
        } else {
            $kind = isset($_POST['sportiq_kind']) ? sanitize_key(wp_unslash($_POST['sportiq_kind'])) : '';
            $value = isset($_POST['sportiq_value']) ? sanitize_text_field(wp_unslash($_POST['sportiq_value'])) : '';
            $value = trim($value);
            if (!isset(sportiq_favorites_options()[$kind]) || $value === '' || strlen($value) > 100) {
                $message = '<p role="alert">팀 또는 리그 정보를 확인하세요.</p>';
            } else {
                $saved = sportiq_favorites_read($uid);
                $key = $kind . ':' . $value;
                if ($_POST['sportiq_fav_action'] === 'remove') {
                    unset($saved[$key]);
                } elseif ($_POST['sportiq_fav_action'] === 'add' && count($saved) < 50) {
                    $saved[$key] = array('kind' => $kind, 'value' => $value);
                }
                update_user_meta($uid, 'sportiq_favorites_v1', $saved);
                $message = '<p role="status">관심 목록을 저장했습니다.</p>';
            }
        }
    }
    $out = '<section class="sportiq-favorites"><h2>내 관심 팀·리그</h2>' . $message;
    $out .= '<form method="post">' . wp_nonce_field('sportiq_favorites_' . $uid, 'sportiq_fav_nonce', true, false);
    $out .= '<label for="sportiq-kind">유형</label> <select id="sportiq-kind" name="sportiq_kind"><option value="team">팀</option><option value="league">리그</option></select> ';
    $out .= '<label for="sportiq-value">이름</label> <input id="sportiq-value" name="sportiq_value" maxlength="100" required> ';
    $out .= '<button name="sportiq_fav_action" value="add" type="submit">저장</button></form>';
    $saved = sportiq_favorites_read($uid);
    if (!$saved) { return $out . '<p>저장한 관심 항목이 없습니다.</p></section>'; }
    $out .= '<ul>';
    foreach ($saved as $item) {
        if (!is_array($item) || !isset($item['kind'], $item['value'])) { continue; }
        $kind = (string) $item['kind']; $value = (string) $item['value'];
        if (!isset(sportiq_favorites_options()[$kind])) { continue; }
        $out .= '<li>' . esc_html(sportiq_favorites_options()[$kind] . ': ' . $value);
        $out .= ' <form method="post" style="display:inline">' . wp_nonce_field('sportiq_favorites_' . $uid, 'sportiq_fav_nonce', true, false);
        $out .= '<input type="hidden" name="sportiq_kind" value="' . esc_attr($kind) . '"><input type="hidden" name="sportiq_value" value="' . esc_attr($value) . '">';
        $out .= '<button type="submit" name="sportiq_fav_action" value="remove">삭제</button></form></li>';
    }
    return $out . '</ul></section>';
}
add_shortcode('sportiq_favorites', 'sportiq_favorites_shortcode');
