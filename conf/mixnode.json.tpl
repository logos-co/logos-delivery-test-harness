{
  "mode": "Core",
  "cluster-id": @CLUSTER@,
  "num-shards-in-network": @SHARDS@,
  "plugin-kad-discovery": true,
  "nodekey": "@NODEKEY@",
  "mix": true,
  "mixkey": "@MIXKEY@",
  "mixnode": [@MIXNODES@],
  "lightpush": true,
  @MIX_ALLOW_ALL@
  @MIX_KEY_ROTATION@
  @MIX_RLN@
  "kad-bootstrap-node": [@BOOTSTRAPS@],
  "kad-service-lookup-interval": @LOOKUP@,
  "discv5-discovery": false,
  "rendezvous": false,
  "tcp-port": @TCP_PORT@,
  "nat": "extip:@IP@",
  "log-level": "DEBUG"
}
