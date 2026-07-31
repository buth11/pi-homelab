resource "kubernetes_namespace" "uptime_kuma" {
  metadata {
    name = "uptime-kuma"
  }
}

resource "kubernetes_persistent_volume_claim" "uptime_kuma_config" {
  wait_until_bound = false
  metadata {
    name      = "uptime-kuma-config"
    namespace = kubernetes_namespace.uptime_kuma.metadata[0].name
  }
  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = "local-path"
    resources {
      requests = {
        storage = "1Gi"
      }
    }
  }
}

resource "kubernetes_deployment" "uptime_kuma" {
  metadata {
    name      = "uptime-kuma"
    namespace = kubernetes_namespace.uptime_kuma.metadata[0].name
    labels = {
      app = "uptime-kuma"
    }
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        app = "uptime-kuma"
      }
    }
    template {
      metadata {
        labels = {
          app = "uptime-kuma"
        }
      }
      spec {
        node_selector = {
          "kubernetes.io/hostname" = "g3-worker3"
        }
        container {
          name  = "uptime-kuma"
          image = "louislam/uptime-kuma:1"
          port {
            container_port = 3001
            name           = "webui"
          }
          volume_mount {
            name       = "data"
            mount_path = "/app/data"
          }
        }
        volume {
          name = "data"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim.uptime_kuma_config.metadata[0].name
          }
        }
      }
    }
  }
}

resource "kubernetes_service" "uptime_kuma" {
  metadata {
    name      = "uptime-kuma"
    namespace = kubernetes_namespace.uptime_kuma.metadata[0].name
  }
  spec {
    type = "LoadBalancer"
    selector = {
      app = "uptime-kuma"
    }
    port {
      port        = 3001
      target_port = 3001
    }
  }
  lifecycle {
    ignore_changes = [metadata[0].annotations]
  }
}
