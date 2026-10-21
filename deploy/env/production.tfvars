regions = ["us-east-1", "sa-east-1"] # one deploy per region; Cloudflare fails over in this order
hosts   = ["sales.joshuel.com.br"]

cpu          = 256
memory       = 512
min_capacity = 2
max_capacity = 4
