from openerp.tests.common import TransactionCase

class TestProductIdChange(TransactionCase):
    """Test that when an included tax is mapped by a fiscal position, the included tax must be
    subtracted to the price of the product.
    """

    def setUp(self):
        super(TestProductIdChange, self).setUp()
        self.fiscal_position_model = self.registry('account.fiscal.position')
        self.fiscal_position_tax_model = self.registry('account.fiscal.position.tax')
        self.tax_model = self.registry('account.tax')
        self.pricelist_model = self.registry('product.pricelist')
        self.res_partner_model = self.registry('res.partner')
        self.product_tmpl_model = self.registry('product.template')
        self.product_model = self.registry('product.product')
        self.invoice_line_model = self.registry('account.invoice.line')

    def test_product_id_change(self):
        cr, uid = self.cr, self.uid
        partner_id = self.res_partner_model.create(cr, uid, dict(name="George"))
        tax_include_id = self.tax_model.create(cr, uid, dict(name="Include tax",
                                                             type='percent',
                                                             amount='0.21',
                                                             price_include=True))
        tax_exclude_id = self.tax_model.create(cr, uid, dict(name="Exclude tax",
                                                             type='percent',
                                                             amount='0.00'))
        product_tmpl_id = self.product_tmpl_model.create(cr, uid, dict(name="Voiture",
                                                                       list_price='121',
                                                                       standard_price='121',
                                                                       taxes_id=[(6, 0, [tax_include_id])],
                                                                       supplier_taxes_id=[(6, 0, [tax_include_id])]))
        product_id = self.product_model.create(cr, uid, dict(product_tmpl_id=product_tmpl_id))
        product = self.product_model.browse(cr, uid, [product_id])
        fp_id = self.fiscal_position_model.create(cr, uid, dict(name="fiscal position",
                                                                sequence=1))
        fp_tax_id = self.fiscal_position_tax_model.create(cr, uid, dict(position_id=fp_id,
                                                                        tax_src_id=tax_include_id,
                                                                        tax_dest_id=tax_exclude_id))

        res = self.invoice_line_model.product_id_change(cr, uid, [], product.id, product.uom_id.id,
                                                        qty=1, type='out_invoice', partner_id=partner_id,
                                                        fposition_id=fp_id)
        self.assertEqual(100, res['value']['price_unit'], "The included tax must be subtracted to the price")

        res = self.invoice_line_model.product_id_change(cr, uid, [], product.id, product.uom_id.id,
                                                        qty=1, type='in_invoice', partner_id=partner_id,
                                                        fposition_id=fp_id)
        self.assertEqual(100, res['value']['price_unit'], "The included tax must be subtracted to the price")
