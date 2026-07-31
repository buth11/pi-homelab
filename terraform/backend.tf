terraform {
  backend "s3" {
    bucket                      = "terraform-state"
    key                         = "pi-homelab/terraform.tfstate"
    region                      = "us-east-1"
    endpoints                   = { s3 = "http://192.168.50.64:9000" }
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    use_path_style              = true
  }
}
