{
    'name': 'TerraKernel Squeeler',
    'summary': 'Run raw SQL through the Odoo external API',
    'description': """
Expose PostgreSQL to API clients through two model methods on ``tk.squeeler``:

* ``execute_read`` runs one statement in a separate, READ ONLY transaction with
  a statement timeout and a row cap. PostgreSQL itself rejects any write.
* ``execute_write`` runs one statement on the request cursor, inside the
  caller's transaction, bypassing the ORM.

Each method is gated by its own group, and every call is recorded in an audit
log, including the ones that fail.

Administrators can also save read-only query templates. API clients run them
by code through ``tk.squeeler.template.run(code, params)``, with typed, named
parameters, and need only one of the template's allowed groups, not raw SQL.
""",
    'version': '20.0.1.0.0',
    'category': 'Technical',
    'author': 'TerraKernel',
    'website': 'https://github.com/terrakernel/tk-squeeler',
    'license': 'LGPL-3',
    'depends': ['base'],
    'data': [
        'security/squeeler_groups.xml',
        'security/ir.access.csv',
        'data/squeeler_data.xml',
        'views/squeeler_template_views.xml',
        'views/squeeler_log_views.xml',
        'views/squeeler_menus.xml',
    ],
    'installable': True,
    'application': False,
}
