resource "kubernetes_namespace" "minio" {
  metadata {
    name = "minio"
  }
}

resource "kubernetes_persistent_volume_claim" "minio_data" {
  metadata {
    name      = "minio-data"
    namespace = kubernetes_namespace.minio.metadata[0].name
  }
  wait_until_bound = false
  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = "local-path"
    resources {
      requests = {
        storage = "20Gi"
      }
    }
  }
}

resource "kubernetes_secret" "minio_credentials" {
  metadata {
    name      = "minio-credentials"
    namespace = kubernetes_namespace.minio.metadata[0].name
  }
  data = {
    root-user     = "admin"
    root-password = var.minio_root_password
  }
}

resource "kubernetes_deployment" "minio" {
  metadata {
    name      = "minio"
    namespace = kubernetes_namespace.minio.metadata[0].name
    labels = {
      app = "minio"
    }
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "minio"
      }
    }
    template {
      metadata {
        labels = {
          app = "minio"
        }
      }
      spec {
        node_selector = {
          "kubernetes.io/hostname" = "g3-worker3"
        }
        container {
          name  = "minio"
          image = "minio/minio:latest"
          args  = ["server", "/data", "--console-address", ":9001"]

          env {
            name = "MINIO_ROOT_USER"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.minio_credentials.metadata[0].name
                key  = "root-user"
              }
            }
          }
          env {
            name = "MINIO_ROOT_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.minio_credentials.metadata[0].name
                key  = "root-password"
              }
            }
          }

          port {
            container_port = 9000
            name           = "api"
          }
          port {
            container_port = 9001
            name           = "console"
          }
          volume_mount {
            name       = "data"
            mount_path = "/data"
          }
        }
        volume {
          name = "data"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim.minio_data.metadata[0].name
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [spec[0].template[0].metadata[0].annotations]
  }
}

resource "kubernetes_service" "minio" {
  metadata {
    name      = "minio"
    namespace = kubernetes_namespace.minio.metadata[0].name
  }
  spec {
    type = "LoadBalancer"
    selector = {
      app = "minio"
    }
    port {
      name        = "api"
      port        = 9000
      target_port = 9000
    }
    port {
      name        = "console"
      port        = 9001
      target_port = 9001
    }
  }
  lifecycle {
    ignore_changes = [metadata[0].annotations]
  }
}
