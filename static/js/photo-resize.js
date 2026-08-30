/*
 * Auto-resizes any file selected in an <input type="file" data-autoresize>
 * down to a max dimension before the form is submitted. This exists because
 * modern phone cameras (especially iPhone) produce photos that can be
 * 8-15MB+, which used to get silently rejected mid-upload on Safari,
 * showing "this page cannot be opened" instead of a clean error.
 *
 * Resizing happens entirely in the browser (canvas), so uploads are also
 * much faster on mobile data. Non-image files / unsupported browsers are
 * left untouched and simply upload as-is (server-side validation still
 * applies as a safety net).
 */
(function () {
  var MAX_DIMENSION = 1600;
  var JPEG_QUALITY = 0.85;

  function resizeFile(file) {
    return new Promise(function (resolve) {
      if (!file || !file.type || file.type.indexOf("image/") !== 0) {
        resolve(file);
        return;
      }
      // Small files are already fine — skip the work.
      if (file.size <= 1.5 * 1024 * 1024) {
        resolve(file);
        return;
      }
      var img = new Image();
      var url = URL.createObjectURL(file);
      img.onload = function () {
        URL.revokeObjectURL(url);
        var w = img.width, h = img.height;
        if (w > h && w > MAX_DIMENSION) {
          h = Math.round((h * MAX_DIMENSION) / w);
          w = MAX_DIMENSION;
        } else if (h > MAX_DIMENSION) {
          w = Math.round((w * MAX_DIMENSION) / h);
          h = MAX_DIMENSION;
        }
        var canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        var ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0, w, h);
        canvas.toBlob(
          function (blob) {
            if (!blob) {
              resolve(file);
              return;
            }
            var newName = file.name.replace(/\.(heic|heif|png|webp)$/i, ".jpg");
            if (!/\.jpe?g$/i.test(newName)) newName += ".jpg";
            var resized = new File([blob], newName, { type: "image/jpeg" });
            resolve(resized);
          },
          "image/jpeg",
          JPEG_QUALITY
        );
      };
      img.onerror = function () {
        URL.revokeObjectURL(url);
        resolve(file); // fall back to original; server still validates
      };
      img.src = url;
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var inputs = document.querySelectorAll('input[type="file"][data-autoresize]');
    inputs.forEach(function (input) {
      var form = input.closest("form");
      if (!form) return;
      form.addEventListener("submit", function (evt) {
        if (input._resizeDone || !input.files || !input.files[0]) return;
        evt.preventDefault();
        var submitBtn = form.querySelector('button[type="submit"], input[type="submit"]');
        if (submitBtn) submitBtn.disabled = true;
        resizeFile(input.files[0]).then(function (resized) {
          try {
            var dt = new DataTransfer();
            dt.items.add(resized);
            input.files = dt.files;
          } catch (e) {
            // Some old browsers don't support DataTransfer reassignment —
            // just submit the original file, server-side limits still apply.
          }
          input._resizeDone = true;
          if (submitBtn) submitBtn.disabled = false;
          form.submit();
        });
      });
    });
  });
})();
