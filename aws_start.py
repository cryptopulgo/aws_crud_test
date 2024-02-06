import json
import boto3

# Cargar credenciales y configurar sesión
json_file_path = './data/config.json'
with open(json_file_path, 'r') as json_file:
    credentials = json.load(json_file)

session = boto3.Session(
    aws_access_key_id=credentials['aws_access_key_id'],
    aws_secret_access_key=credentials['aws_secret_access_key'],
    region_name=credentials['region'],
)

ec2 = session.resource('ec2')
ec2_client = session.client('ec2')

# Comprobar si ya existe una VPC con el CIDRBlock deseado
desired_cidr_block = '10.0.0.0/16'
existing_vpcs = list(ec2.vpcs.filter(Filters=[{'Name': 'cidr', 'Values': [desired_cidr_block]}]))

if existing_vpcs:
    print(f'Ya existe una VPC con el CIDRBlock {desired_cidr_block}: {existing_vpcs[0].id}')
    vpc = existing_vpcs[0]  # Usar la VPC existente
else:
    # Crear una nueva VPC si no existe ninguna con el CIDRBlock deseado
    vpc = ec2.create_vpc(CidrBlock=desired_cidr_block)
    vpc.create_tags(Tags=[{"Key": "Name", "Value": "vpc_crm"}])
    vpc.wait_until_available()
    print(f'VPC Creada: {vpc.id}')

    # Crear un Internet Gateway
    igw = ec2.create_internet_gateway()
    igw.create_tags(Tags=[{"Key": "Name", "Value": "igw_crm"}])
    print(f'Internet Gateway Creado: {igw.id}')

    # Adjuntar el Internet Gateway a la VPC
    vpc.attach_internet_gateway(InternetGatewayId=igw.id)
    print(f'Internet Gateway {igw.id} adjuntado a la VPC {vpc.id}')

    # Obtener la tabla de rutas principal de la VPC
    main_route_table = list(vpc.route_tables.all())[0]
    print(f'Usando la tabla de rutas principal: {main_route_table.id}')

    # Crear una ruta en la tabla de rutas para dirigir todo el tráfico hacia el Internet Gateway
    main_route_table.create_route(
        DestinationCidrBlock='0.0.0.0/0',
        GatewayId=igw.id
    )
    print(f'Ruta hacia Internet añadida a la tabla de rutas {main_route_table.id}')

# Obtener zonas de disponibilidad en la región
azs = ec2_client.describe_availability_zones(Filters=[{'Name': 'region-name', 'Values': [credentials['region']]}])
subnet_index = 1  # Comienza el índice para la parte 'x' del CIDR block

for az in azs['AvailabilityZones']:
    az_name = az['ZoneName']

    # Verificar si ya existe una subred en esta AZ dentro de la VPC
    existing_subnets = list(vpc.subnets.filter(Filters=[{'Name': 'availabilityZone', 'Values': [az_name]}]))

    if existing_subnets:
        print(f'Ya existe una subred en {az_name}: {existing_subnets[0].id}')
    else:
        # Generar un CIDR block único para la nueva subred
        subnet_cidr = f'10.0.{subnet_index}.0/24'

        # Crear una nueva subred si no existe ninguna en esta AZ
        subnet = ec2.create_subnet(CidrBlock=subnet_cidr, VpcId=vpc.id, AvailabilityZone=az_name)
        print(f'Subred Creada en {az_name}: {subnet.id}')

        subnet_index += 1  # Incrementar el índice para el próximo CIDR block

# Comprobar si ya existe un grupo de seguridad con el nombre deseado en la VPC
security_group_name = 'security_group_crm'
existing_security_groups = list(vpc.security_groups.filter(Filters=[{'Name': 'group-name', 'Values': [security_group_name]}]))

if existing_security_groups:
    print(f'Ya existe un grupo de seguridad con el nombre {security_group_name}: {existing_security_groups[0].id}')
    sec_group = existing_security_groups[0]  # Usar el grupo de seguridad existente
else:
    # Crear un nuevo grupo de seguridad si no existe ninguno con el nombre deseado
    sec_group = ec2.create_security_group(GroupName=security_group_name, Description='security_group1_crm', VpcId=vpc.id)
    sec_group.authorize_ingress(CidrIp='0.0.0.0/0', IpProtocol='TCP', FromPort=80, ToPort=80)
    print(f'Grupo de Seguridad Creado: {sec_group.id}')

subnet_ids = [subnet.id for az in azs['AvailabilityZones'] for subnet in vpc.subnets.filter(Filters=[{'Name': 'availabilityZone', 'Values': [az['ZoneName']]}])]

rds = boto3.client(
    'rds',
    region_name=credentials['region'],
    aws_access_key_id=credentials['aws_access_key_id'],
    aws_secret_access_key=credentials['aws_secret_access_key']
)

db_subnet_group_name = 'mi-db-subnet-group'

try:
    # Intenta crear el DB Subnet Group con las subredes recién creadas
    response = rds.create_db_subnet_group(
        DBSubnetGroupName=db_subnet_group_name,
        DBSubnetGroupDescription='DB subnet group para RDS en subredes específicas',
        SubnetIds=subnet_ids
    )
    print(f"DB Subnet Group creado: {response['DBSubnetGroup']['DBSubnetGroupName']}")
except rds.exceptions.DBSubnetGroupAlreadyExistsFault:
    print(f"El DB Subnet Group '{db_subnet_group_name}' ya existe.")
except Exception as e:
    print(f"Error al crear el DB Subnet Group: {e}")

# Comprobar si ya existe una instancia de RDS con el identificador deseado
try:
    response = rds.describe_db_instances(DBInstanceIdentifier='crm-db')
    print(f'Ya existe una instancia de RDS con el identificador "crm-db": {response["DBInstances"][0]["DBInstanceIdentifier"]}')
except rds.exceptions.DBInstanceNotFoundFault:
    # Crear una nueva instancia de RDS si no existe ninguna con el identificador deseado
    try:
        response = rds.create_db_instance(
            DBInstanceIdentifier='crm-db',
            MasterUsername='admin_crm',
            MasterUserPassword='Unj3di3nmipc&',
            DBInstanceClass='db.t2.micro',
            Engine='mysql',
            AllocatedStorage=20,  # Tamaño inicial del almacenamiento en GB
            MaxAllocatedStorage=100,  # Límite máximo de almacenamiento para auto scaling en GB
            VpcSecurityGroupIds=[sec_group.id],
            DBSubnetGroupName=db_subnet_group_name,  # Nombre del DB Subnet Group creado
            MultiAZ=True  # Habilitar despliegue Multi-AZ para alta disponibilidad
        )
        print(f'Base de Datos MySQL Multi-AZ con Auto Scaling Creada: {response["DBInstance"]["DBInstanceIdentifier"]}')
    except Exception as e:
        print(f"Error al crear la instancia de MySQL Multi-AZ con Auto Scaling: {e}")

