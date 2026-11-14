<?php
/**
 * Public SportIQ prediction shortcode and AJAX handler.
 * Usage: [sportiq_prediction match_id="123"] (SportIQ matches.id).
 */
if (!defined('ABSPATH')) { exit; }

if (!function_exists('sportiq_prediction_card')) {
function sportiq_prediction_card($data) {
    if (!isset($data['probabilities']) || !is_array($data['probabilities'])) {
        return '<div class="sportiq-notice">예측 데이터 형식이 올바르지 않습니다.</div>';
    }
    $p = $data['probabilities'];
    foreach (array('home', 'draw', 'away') as $key) {
        if (!isset($p[$key]) || !is_numeric($p[$key]) || !is_finite((float)$p[$key])
            || (float)$p[$key] < 0 || (float)$p[$key] > 1) {
            return '<div class="sportiq-notice">예측 확률을 표시할 수 없습니다.</div>';
        }
    }
    $stale = !empty($data['_meta']['stale']);
    $html = '<div class="sportiq-card"><div class="sportiq-probabilities">';
    $labels = array('home' => '홈 승', 'draw' => '무승부', 'away' => '원정 승');
    foreach ($labels as $key => $label) {
        $html .= '<span>' . esc_html($label) . ': <strong>'
            . esc_html(number_format((float)$p[$key] * 100, 1)) . '%</strong></span> ';
    }
    $html .= '</div>';
    if ($stale) {
        $html .= '<small class="sportiq-stale-note">이전 분석 결과입니다. 최신 정보와 다를 수 있습니다.</small>';
    }
    return $html . '</div>';
}
}

if (!function_exists('sportiq_prediction_shortcode')) {
function sportiq_prediction_shortcode($atts) {
    $atts = shortcode_atts(array('match_id' => 0), $atts, 'sportiq_prediction');
    $id = absint($atts['match_id']);
    if (!$id) {
        return '<div class="sportiq-notice">SportIQ 경기 ID(match_id)가 필요합니다.</div>';
    }
    // Avoid blocking page rendering on a cold cache.
    $cached = get_transient('sportiq_pred_' . $id);
    if (is_array($cached)) {
        $cached['_meta'] = array('source' => 'cache', 'stale' => false);
        return sportiq_prediction_card($cached);
    }
    return '<div class="sportiq-async-container" data-match-id="' . esc_attr($id)
        . '" data-nonce="' . esc_attr(wp_create_nonce('sportiq_fetch_nonce'))
        . '"><p class="sportiq-loading">예측 데이터를 불러오는 중입니다...</p></div>';
}
add_shortcode('sportiq_prediction', 'sportiq_prediction_shortcode');
}

if (!function_exists('sportiq_ajax_get_prediction')) {
function sportiq_ajax_get_prediction() {
    check_ajax_referer('sportiq_fetch_nonce', 'security');
    $id = isset($_POST['match_id']) && is_scalar($_POST['match_id'])
        ? absint(wp_unslash($_POST['match_id'])) : 0;
    if (!$id) {
        wp_send_json_error(array('message' => '유효한 경기 ID가 필요합니다.'), 400);
    }
    $client = new SportIQ_API_Client();
    $result = $client->get_prediction($id);
    if (isset($result['status']) && $result['status'] === 'error') {
        wp_send_json_error(array('message' => $result['message']), 503);
    }
    wp_send_json_success(array(
        'html' => sportiq_prediction_card($result),
        'meta' => isset($result['_meta']) ? $result['_meta'] : array(),
    ));
}
add_action('wp_ajax_sportiq_get_prediction', 'sportiq_ajax_get_prediction');
add_action('wp_ajax_nopriv_sportiq_get_prediction', 'sportiq_ajax_get_prediction');
}

if (!function_exists('sportiq_prediction_footer_script')) {
function sportiq_prediction_footer_script() {
    if (!is_singular()) { return; }
    ?>
    <script>
    (function () {
      function loadPredictions() {
        document.querySelectorAll('.sportiq-async-container:not([data-loaded])').forEach(function (el) {
          el.dataset.loaded = 'true';
          var body = new FormData();
          body.append('action', 'sportiq_get_prediction');
          body.append('match_id', el.dataset.matchId);
          body.append('security', el.dataset.nonce);
          fetch(<?php echo wp_json_encode(admin_url('admin-ajax.php')); ?>, {
            method: 'POST', credentials: 'same-origin', body: body
          }).then(function (response) { return response.json(); })
            .then(function (result) {
              if (result.success && result.data && typeof result.data.html === 'string') {
                el.innerHTML = result.data.html; // Trusted server-generated escaped card HTML.
              } else {
                el.textContent = (result.data && result.data.message)
                  ? String(result.data.message) : '예측 데이터를 불러올 수 없습니다.';
              }
            }).catch(function () {
              el.textContent = '네트워크 연결이 지연되고 있습니다.';
            });
        });
      }
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', loadPredictions);
      } else { loadPredictions(); }
    })();
    </script>
    <?php
}
add_action('wp_footer', 'sportiq_prediction_footer_script');
}
