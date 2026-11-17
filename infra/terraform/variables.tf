variable "subscription_id" {
  description = "ID da assinatura Azure (az account show --query id -o tsv)"
  type        = string
}

variable "location" {
  description = "Regiao Azure. brazilsouth mantem os dados no Brasil (LGPD) e perto do cidadao."
  type        = string
  default     = "brazilsouth"
}

variable "app_name" {
  description = "Nome global do web app: <app_name>.azurewebsites.net"
  type        = string
  default     = "contaclara-gs"
}

variable "sku" {
  description = "Plano do App Service. F1 = gratuito (60 min de CPU/dia, sem Always On)."
  type        = string
  default     = "F1"
}

variable "admin_cidr" {
  description = "IP/CIDR do administrador liberado no SCM/Kudu (porta administrativa), ex.: 200.100.50.25/32"
  type        = string

  validation {
    # Barra 0.0.0.0/0 e qualquer faixa larga: so aceita prefixo /24 a /32
    condition     = can(cidrhost(var.admin_cidr, 0)) && can(regex("/(2[4-9]|3[0-2])$", var.admin_cidr))
    error_message = "admin_cidr deve ser um CIDR de /24 a /32 (ex.: 200.100.50.25/32). 0.0.0.0/0 e proibido."
  }
}

variable "api_image" {
  description = "Imagem publicada pelo pipeline no GHCR, sem registro e sem tag, em minusculas: <login-github>/conta-clara"
  type        = string
}

variable "github_repo" {
  # Formato do subject OIDC atual do GitHub: owner@id/repo@id (IDs imutaveis: renomear ou recriar o repo nao herda o acesso).
  # IDs: gh api repos/<login>/conta-clara --jq '"\(.owner.login)@\(.owner.id)/\(.name)@\(.id)"'
  description = "Repositorio autorizado a fazer deploy via OIDC (owner@id/nome@id)"
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9-]+@[0-9]+/[A-Za-z0-9._-]+@[0-9]+$", var.github_repo))
    error_message = "Use o formato owner@id/repo@id (ver comando no comentario da variavel)."
  }
}
