// Auto-dismiss flash messages after 4 seconds
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.alert.alert-success, .alert.alert-info').forEach(function (el) {
    setTimeout(function () {
      const bsAlert = bootstrap.Alert.getOrCreateInstance(el);
      if (bsAlert) bsAlert.close();
    }, 4000);
  });

  // Enforce start < end on any time pair in forms
  document.querySelectorAll('form').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      const st = form.querySelector('[name="start_time"]');
      const et = form.querySelector('[name="end_time"]');
      if (st && et && st.value && et.value && st.value >= et.value) {
        e.preventDefault();
        alert(document.documentElement.lang === 'ja'
          ? '開始時刻は終了時刻より前にしてください。'
          : 'Start time must be before end time.');
        st.focus();
      }
    });
  });
});
