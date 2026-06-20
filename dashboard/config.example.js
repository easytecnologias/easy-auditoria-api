// Copie este arquivo para config.js e preencha com os valores do ambiente
// NÃO commite o config.js no git (está no .gitignore)
window.APP_CONFIG = {
  // Em HTTPS: usar proxy nginx /streamer/ para evitar mixed content
  // Em HTTP local: pode usar http://SEU_PDV1_IP:8765 diretamente
  STREAMER_URL:   "/streamer",
  STREAMER_TOKEN: "SEU_AUDITORIA_API_TOKEN",
};
