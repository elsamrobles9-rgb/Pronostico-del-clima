"use strict";
// Confirmaciones antes de acciones delicadas y botón para cerrar alertas (sin scripts en línea por la CSP).
document.addEventListener("submit", function (e) {
  var mensaje = e.target.getAttribute("data-confirmar");
  if (mensaje && !window.confirm(mensaje)) e.preventDefault();
});
document.addEventListener("click", function (e) {
  var boton = e.target.closest ? e.target.closest("[data-cerrar-alerta]") : null;
  if (boton) boton.parentNode.remove();
});
