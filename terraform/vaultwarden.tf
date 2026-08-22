resource "kubernetes_namespace" "vaultwarden" {
  metadata {
    name = "vaultwarden"
  }
}

resource "kubernetes_persistent_volume_claim" "vaultwarden_data" {
  metadata {
    name      = "vaultwarden-data"
    namespace = kubernetes_namespace.vaultwarden.metadata[0].name
  }
  wait_until_bound = false
  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = "local-path"
    resources {
      requests = {
        storage = "2Gi"
      }
    }
  }
}

resource "kubernetes_secret" "vaultwarden_admin" {
  metadata {
    name      = "vaultwarden-admin"
    namespace = kubernetes_namespace.vaultwarden.metadata[0].name
  }
  data = {
    admin-token = var.vaultwarden_admin_token
  }
}

resource "kubernetes_deployment" "vaultwarden" {
  metadata {
    name      = "vaultwarden"
    namespace = kubernetes_namespace.vaultwarden.metadata[0].name
    labels = {
      app = "vaultwarden"
    }
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "vaultwarden"
      }
    }
    template {
      metadata {
        labels = {
          app = "vaultwarden"
        }
      }
      spec {
        node_selector = {
          "kubernetes.io/hostname" = "g3-worker3"
        }
        container {
          name  = "vaultwarden"
          image = "vaultwarden/server:latest"

          env {
            name = "ADMIN_TOKEN"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.vaultwarden_admin.metadata[0].name
                key  = "admin-token"
              }
            }
          }
          env {
            name  = "SIGNUPS_ALLOWED"
            value = "false"
          }
          env {
            name  = "WEBSOCKET_ENABLED"
            value = "true"
          }

          port {
            container_port = 80
            name            = "http"
          }
          volume_mount {
            name       = "data"
            mount_path = "/data"
          }
        }
        volume {
          name = "data"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim.vaultwarden_data.metadata[0].name
          }
        }
      }
    }
  }
}

resource "kubernetes_service" "vaultwarden" {
  metadata {
    name      = "vaultwarden"
    namespace = kubernetes_namespace.vaultwarden.metadata[0].name
  }
  spec {
    type = "ClusterIP"
    selector = {
      app = "vaultwarden"
    }
    port {
      port        = 80
      target_port = 80
    }
  }
}
