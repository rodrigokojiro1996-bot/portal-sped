// Pop-up do resultado da publicação. O servidor já o entrega aberto (<dialog open>), e o botão
// "OK, entendi" fecha sem JavaScript. Aqui ele é reaberto como modal: o navegador passa a fechá-lo
// com a tecla Esc, prende o foco no pop-up e bloqueia cliques no fundo.
document.querySelectorAll("dialog.publish-dialog[open]").forEach(function (dialog) {
  if (typeof dialog.showModal === "function") {
    dialog.close();
    dialog.showModal();
  }
});

// Garantia para navegadores sem modal nativo: Esc fecha qualquer pop-up de publicação aberto.
document.addEventListener("keydown", function (event) {
  if (event.key !== "Escape") {
    return;
  }
  document.querySelectorAll("dialog.publish-dialog[open]").forEach(function (dialog) {
    dialog.close();
  });
});
