Change pihole password
kubectl exec -n pihole deployment/pihole -- pihole setpassword 'password'