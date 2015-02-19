#!/usr/bin/env python
# -*- coding: utf-8 -*-

# This module creates the babysitter instance and services

import json

from troposphere import Parameter, Ref, FindInMap, Base64, GetAtt, Tags
import troposphere.autoscaling as autoscaling
from troposphere.autoscaling import EC2_INSTANCE_TERMINATE
import troposphere.cloudwatch as cloudwatch
import troposphere.ec2 as ec2
import troposphere.sns as sns
import troposphere.sqs as sqs
import troposphere.iam as iam

import config as cfn
from config import CIDR_PREFIX, VPC_NAME, CLOUDNAME, CLOUDENV, ASSUME_ROLE_POLICY, template
from config import USE_PRIVATE_SUBNETS, DEFAULT_ROUTE

def emit_configuration():
    # Parameters here
    babysitter_instance_class = template.add_parameter(
        Parameter(
            'BabysitterInstanceType', Type='String', Default='t2.micro',
            Description='Chef babysitter instance type',
            AllowedValues=cfn.usable_instances(),
            ConstraintDescription='Instance size must be a valid instance type'
        )
    )

    babysitter_email_param = template.add_parameter(
        Parameter(
            'BabysitterAlarmEmail',
            Default='wlee@leaf.me',
            Description='Email address to notify if tehre are issues in the babysitter queue',
            Type='String'
        )
    )

    # Build an SQS queue for the babysitter
    queue_name = '_'.join(['chef-deregistration', CLOUDNAME, CLOUDENV])
    queue = template.add_resource(
        sqs.Queue(
            cfn.sanitize_id(queue_name),
            VisibilityTimeout=60,
            MessageRetentionPeriod=1209600,
            MaximumMessageSize=16384,
            QueueName=queue_name
        )
    )

    alert_topic = template.add_resource(
        sns.Topic(
            "BabysitterAlarmTopic",
            DisplayName='Babysitter Alarm',
            TopicName=queue_name,
            Subscription=[
                sns.Subscription(
                    Endpoint=Ref(babysitter_email_param),
                    Protocol='email'
                ),
            ],
            DependsOn=queue.title
        )
    )

    queue_depth_alarm = template.add_resource(
        cloudwatch.Alarm(
            "BabysitterQueueDepthAlarm",
            AlarmDescription='Alarm if the queue depth grows beyond 200 messages',
            Namespace='AWS/SQS',
            MetricName='ApproximateNumberOfMessagesVisible',
            Dimensions=[
                cloudwatch.MetricDimension(
                    Name='QueueName',
                    Value=GetAtt(queue, "QueueName")
                )
            ],
            Statistic='Sum',
            Period='300',
            EvaluationPeriods='1',
            Threshold='200',
            ComparisonOperator='GreaterThanThreshold',
            AlarmActions=[Ref(alert_topic), ],
            InsufficientDataActions=[Ref(alert_topic), ],
            DependsOn=alert_topic.title
        ),
    )

    # babysitter IAM role
    babysitter_role_name = '.'.join(['babysitter', CLOUDNAME, CLOUDENV])
    babysitter_iam_role = template.add_resource(
        iam.Role(
            'BabysitterIamRole',
            AssumeRolePolicyDocument=ASSUME_ROLE_POLICY,
            Path="/",
            Policies=[
                iam.Policy(
                    PolicyName='BabySitterPolicy',
                    PolicyDocument=json.loads(cfn.load_template("babysitter_policy.json.j2",
                        {"env": CLOUDENV, "cloud": CLOUDNAME, "region": "us-east-1"}
                    ))
                ),
                iam.Policy(
                    PolicyName='BabySitterDefaultPolicy',
                    PolicyDocument=json.loads(cfn.load_template("default_policy.json.j2",
                        {"env": CLOUDENV, "cloud": CLOUDNAME, "region": "us-east-1"}
                    ))
                )
            ],
            DependsOn=cfn.vpcs[0].title
        )
    )

    babysitter_instance_profile = template.add_resource(
        iam.InstanceProfile(
            "BabysitterInstanceProfile",
            Path="/",
            Roles=[Ref(babysitter_iam_role)],
            DependsOn=babysitter_iam_role.title
        )
    )

    babysitter_user_data = cfn.load_template("default-init.bash.j2",
            {"env": CLOUDENV, "cloud": CLOUDNAME, "deploy": "babysitter"}
    )

    ingress_rules = [
        ec2.SecurityGroupRule(
            IpProtocol='tcp', CidrIp=DEFAULT_ROUTE, FromPort=p, ToPort=p
        ) for p in [22]
    ]

    security_group = template.add_resource(
        ec2.SecurityGroup(
            "BabysitterSecurityGroup",
            GroupDescription='Security Group for babysitter instances',
            VpcId=Ref(cfn.vpcs[0]),
            SecurityGroupIngress=ingress_rules,
            DependsOn=cfn.vpcs[0].title,
            Tags=Tags(Name='.'.join(['babysitter-sg', CLOUDNAME, CLOUDENV]))
        )
    )

    launch_cfg = template.add_resource(
        autoscaling.LaunchConfiguration(
            "BabysitterLaunchConfiguration",
            ImageId=FindInMap('RegionMap', Ref("AWS::Region"), int(cfn.Amis.EBS)),
            InstanceType=Ref(babysitter_instance_class),
            IamInstanceProfile=Ref(babysitter_instance_profile),
            AssociatePublicIpAddress=not USE_PRIVATE_SUBNETS,
            BlockDeviceMappings=[
                ec2.BlockDeviceMapping(
                    DeviceName='/dev/sda1',
                    Ebs=ec2.EBSBlockDevice(
                        DeleteOnTermination=True
                    )
                )
            ],
            KeyName=Ref(cfn.keyname),
            SecurityGroups=[Ref(security_group)],
            DependsOn=[babysitter_instance_profile.title, security_group.title],
            UserData=Base64(babysitter_user_data)
        )
    )

    asg_name = '.'.join(['babysitter', CLOUDNAME, CLOUDENV])
    asg = template.add_resource(
        autoscaling.AutoScalingGroup(
            "BabysitterASG",
            AvailabilityZones=cfn.get_asg_azs(),
            DesiredCapacity="1",
            LaunchConfigurationName=Ref(launch_cfg),
            MinSize="1",
            MaxSize="1",
            NotificationConfiguration=autoscaling.NotificationConfiguration(
                TopicARN=Ref(alert_topic),
                NotificationTypes=[
                    EC2_INSTANCE_TERMINATE
                ]
            ),
            VPCZoneIdentifier=[Ref(sn) for sn in cfn.get_vpc_subnets(cfn.vpcs[0], cfn.SubnetTypes.PLATFORM)]
        )
    )
